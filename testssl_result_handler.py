#!/usr/bin/env python

__author__ = "bitsofinfo"

from multiprocessing import Pool, Process
import random
import json
import pprint
import yaml
from dateutil import parser as dateparser
import re
import os
from objectpath import *
import argparse
import getopt, sys
import datetime
import logging
import requests
import base64
from jinja2 import Template, Environment
import subprocess
import time
from slackclient import SlackClient
from pygrok import Grok

import http.server


import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from http import HTTPStatus
from urllib.parse import urlparse
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY
import concurrent.futures


# Dict of result handler yaml parsed configs (filename -> object)
result_handler_configs = {}

class TestsslResultProcessor(object):

    # for controlling access to job_name_2_metrics_db
    lock = threading.RLock()

    result_handler_configs = {}

    # total threads = total amount of commands
    # per file that can be processed concurrently
    threads = 1

    # The Objectpath Tree for the evaluation_doc JSON
    evaluation_doc_objectpath_tree = None

    def exec_objectpath(self,objectpath_query):
        x = self.evaluation_doc_objectpath_tree.execute(objectpath_query)
        if isinstance(x,(str,bool,int)):
            return x
        elif x is not None:
            return next(x)
        else:
            return None

    # Will process the testssl_json_result_file_path file
    def processResultFile(self,testssl_json_result_file_path,input_dir):

        logging.info("Processing new testssl.sh JSON result file: '%s'", testssl_json_result_file_path)

        # open the file
        testssl_result = None
        try:
            with open(testssl_json_result_file_path) as f:
                testssl_result = json.load(f)
        except:
            logging.error("Unexpected error in open():"+testssl_json_result_file_path, sys.exc_info()[0])

        # for each of our result handler configs
        # lets process the JSON result file through it
        try:
            for config_filename, config in result_handler_configs.items():

                try:
                    # create uberdoc for evaluations
                    evaluation_doc = {
                                config['evaluation_doc_config']['target_keys']['testssl_result_json']: testssl_result,
                                config['evaluation_doc_config']['target_keys']['testssl_result_path']:os.path.dirname(testssl_json_result_file_path).replace(input_dir+"/","")
                              }

                    # apply any properties found in the path_properties_grok
                    if 'path_properties_grok' in config and config['path_properties_grok'] is not None:
                        grok = Grok(config['path_properties_grok'],custom_patterns=config['custom_groks'])
                        matches = grok.match(testssl_json_result_file_path)
                        del matches['ignored']
                        result_metadata = {
                                          config['evaluation_doc_config']['target_keys']['result_metadata']:matches
                                          }
                        evaluation_doc.update(result_metadata)

                    # Create our Tree to do ObjectPath evals
                    # against our evaluation_doc
                    self.evaluation_doc_objectpath_tree = Tree(evaluation_doc)

                    # Lets grab the cert expires to calc number of days till expiration
                    cert_expires_at_str = next(self.evaluation_doc_objectpath_tree.execute(config['cert_expires_objectpath']))
                    cert_expires_at = dateparser.parse(cert_expires_at_str)
                    expires_in_days = cert_expires_at - datetime.datetime.utcnow()
                    evaluation_doc.update({
                                    config['evaluation_doc_config']['target_keys']['cert_expires_in_days']:expires_in_days.days
                                   })


                    # Rebuild our Tree to do ObjectPath evals
                    # against our evaluation_doc to sure the Tree is up to date
                    self.evaluation_doc_objectpath_tree = Tree(evaluation_doc)

                    # Create an Jinja2 Environment
                    # and register a new filter for the exec_objectpath method
                    env = Environment()
                    env.filters['exec_objectpath'] = self.exec_objectpath

                    # Create out standard header text and attachment
                    slack_template = env.from_string(config['alert_engines']['slack']['template'])
                    rendered_template = slack_template.render(evaluation_doc)

                    # Convert to an object we can now append trigger results to
                    slack_data = json.loads(rendered_template)

                    # lets process all triggers
                    triggers_fired = False
                    for trigger_name in config['trigger_on']:
                        trigger = config['trigger_on'][trigger_name]
                        exec_result = self.evaluation_doc_objectpath_tree.execute(trigger['objectpath'])
                        results = []

                        if exec_result is not None:
                            triggers_fired = True

                            if isinstance(exec_result,(str,int,float,bool)):
                                if isinstance(exec_result,(bool)):
                                    if exec_result is False:
                                        continue

                                results.append(exec_result)
                            else:
                                results = list(exec_result)

                            if len(results) > 0:
                                attachment_title = trigger['title']
                                attachment_text = "```\n"
                                for r in results:
                                    attachment_text += json.dumps(r)+"\n"
                                attachment_text += "```\n"
                                slack_data['attachments'].append({'title':attachment_title,'text':attachment_text, 'color':'danger'})

                    if triggers_fired:
                        response = requests.post(
                            config['alert_engines']['slack']['webhook_url'], data=json.dumps(slack_data),
                            headers={'Content-Type': 'application/json'}
                        )
                        if response.status_code != 200:
                            raise ValueError(
                                'Request to slack returned an error %s, the response is:\n%s'
                                % (response.status_code, response.text)
                            )

                except Exception as e:
                    logging.exception("Unexpected error processing: " + testssl_json_result_file_path + " using: " + config_filename + " err:" + str(sys.exc_info()[0]))

        except Exception as e:
            logging.exception("Unexpected error processing: " + testssl_json_result_file_path + " " + str(sys.exc_info()[0]))



class TestsslResultFileMonitor(FileSystemEventHandler):

    # We will feed new input files to this processor
    testssl_result_processor = None

    # max threads
    threads = 1

    # our Pool
    executor = None

    # input_dir_sleep_seconds
    input_dir_sleep_seconds = 300

    # the actual input_dir that we are monitoring
    input_dir = None

    # Filter to match relevent paths in events received
    filename_filter = '.json'

    def set_threads(self, t):
        self.threads = t

    def on_created(self, event):
        super(TestsslResultFileMonitor, self).on_created(event)

        if not self.executor:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.threads)

        if event.is_directory:
            return

        if self.filename_filter in event.src_path:

            logging.info("Responding to creation of testssl.sh JSON result: %s", event.src_path)

            # give write time to close....
            time.sleep(self.input_dir_sleep_seconds)

            self.executor.submit(self.testssl_result_processor.processResultFile,event.src_path,self.input_dir)



class HandlerConfigFileMonitor(FileSystemEventHandler):

    # our Pool
    executor = None

    # Filter to match relevent paths in events received
    filename_filter = '.json'

    def on_created(self, event):
        super(HandlerConfigFileMonitor, self).on_created(event)

        if not self.executor:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        if event.is_directory:
            return

        if '.yaml' in event.src_path:

            logging.info("Responding to creation of result handler config file: %s", event.src_path)

            # attempt to open the config
            # and parse the yaml
            try:
                config = None
                with open(event.src_path, 'r') as stream:
                    try:
                        config = yaml.load(stream)
                    except yaml.YAMLError as exc:
                        logging.error(event.src_path + ": Unexpected error in yaml.load()", sys.exc_info()[0])
            except Exception as e:
                logging.error(event.src_path + ": Unexpected error:", sys.exc_info()[0])

            # our config name is the filename
            config_filename = os.path.basename(event.src_path)
            result_handler_configs[config_filename] = config


def init_watching(input_dir,
                  config_dir,
                  input_dir_watchdog_threads,
                  input_dir_sleep_seconds):

    # mthreaded...
    if (isinstance(input_dir_watchdog_threads,str)):
        input_dir_watchdog_threads = int(input_dir_watchdog_threads)

    # create watchdog to look for new config files
    result_handler_config_monitor = HandlerConfigFileMonitor()

    # create watchdog to look for new files
    event_handler = TestsslResultFileMonitor()
    event_handler.set_threads(input_dir_watchdog_threads)
    event_handler.input_dir = input_dir
    if (isinstance(input_dir_sleep_seconds,str)):
        input_dir_sleep_seconds = int(input_dir_sleep_seconds)
    event_handler.input_dir_sleep_seconds = input_dir_sleep_seconds

    # Create a TestsslProcessor to consume the testssl_cmds files
    event_handler.testssl_result_processor = TestsslResultProcessor()

    # give the processor the total number of threads to use
    # for processing testssl.sh cmds concurrently
    if (isinstance(input_dir_watchdog_threads,str)):
        input_dir_watchdog_threads = int(input_dir_watchdog_threads)
    event_handler.testssl_result_processor.threads = input_dir_watchdog_threads


    # schedule our testssl.sh json result file watchdog
    observer1 = Observer()
    observer1.schedule(result_handler_config_monitor, config_dir, recursive=True)
    observer1.start()

    logging.info("Monitoring for new result handler config YAML files at: %s ",config_dir)

    # schedule our testssl.sh json result file watchdog
    observer2 = Observer()
    observer2.schedule(event_handler, input_dir, recursive=True)
    observer2.start()

    logging.info("Monitoring for new testssl.sh result JSON files at: %s ",input_dir)

    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        observer1.stop()
        observer2.stop()
    observer1.join()
    observer2.join()




###########################
# Main program
##########################
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input-dir', dest='input_dir', default="./input", help="Directory path to recursively monitor for new `*.json` testssl.sh result files")
    parser.add_argument('-I', '--config-dir', dest='config_dir', default="./configs", help="Directory path to recursively monitor for new `*.yaml` result handler config files")
    parser.add_argument('-l', '--log-file', dest='log_file', default=None, help="Path to log file, default None, STDOUT")
    parser.add_argument('-x', '--log-level', dest='log_level', default="DEBUG", help="log level, default DEBUG ")
    parser.add_argument('-w', '--input-dir-watchdog-threads', dest='input_dir_watchdog_threads', default=10, help="max threads for watchdog input-dir file processing, default 10")
    parser.add_argument('-s', '--input-dir-sleep-seconds', dest='input_dir_sleep_seconds', default=300, help="When a new *.json file is detected in --input-dir, how many seconds to wait before processing to allow testssl.sh to finish writing")

    args = parser.parse_args()

    logging.basicConfig(level=logging.getLevelName(args.log_level),
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        filename=args.log_file,filemode='w')
    logging.Formatter.converter = time.gmtime


    init_watching(args.input_dir,
                  args.config_dir,
                  args.input_dir_watchdog_threads,
                  int(args.input_dir_sleep_seconds))
