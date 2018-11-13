#!/usr/bin/env python

__author__ = "bitsofinfo"

import importlib
from multiprocessing import Pool, Process
import json
import pprint
import yaml
from dateutil import parser as dateparser
import re
import os
from objectpath import *
import argparse
import collections
import sys
import datetime
import logging
import time
from pygrok import Grok

import http.server

import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent
import concurrent.futures

# Dict of result handler yaml parsed configs (filename -> object)
result_handler_configs = {}


class ObjectPathContext():

    # The Objectpath Tree for the evaluation_doc JSON
    evaluation_doc_objectpath_tree = None

    # the raw evaluation doc
    evaluation_doc = None

    # More debugging
    debug_objectpath_expr = False
    dump_evaldoc_on_error = False

    def __init__(self, evaldoc, debug_objectpath_expressions, dump_evaldoc_on_error):
        self.debug_objectpath_expr = debug_objectpath_expressions
        self.dump_evaldoc_on_error = dump_evaldoc_on_error
        self.update(evaldoc)

    # update the context w/ the most recent
    # evaluation_doc
    def update(self,evaldoc):
        self.evaluation_doc = evaldoc
        self.evaluation_doc_objectpath_tree = Tree(self.evaluation_doc)

    # Uses ObjectPath to evaluate the given
    # objectpath_query against the current state of the
    # `evaluation_doc_objectpath_tree`
    #
    # NOTE! If multiple matches, returns the 1st match
    def exec_objectpath_first_match(self,objectpath_query):
        return self._exec_objectpath(objectpath_query,0)

    # Uses ObjectPath to evaluate the given
    # objectpath_query against the current state of the
    # `evaluation_doc_objectpath_tree`
    #
    # NOTE! If multiple matches, returns the match located
    # at index `force_return_index_on_multiple_results`
    # unless force_return_index_on_multiple_results=None
    # then returns all
    def exec_objectpath_specific_match(self,objectpath_query,force_return_index_on_multiple_results=None):
        return self._exec_objectpath(objectpath_query,force_return_index_on_multiple_results)

    # Uses ObjectPath to evaluate the given
    # objectpath_query against the current state of the
    # `evaluation_doc_objectpath_tree`
    #
    # NOTE! this can return lists of values of when multiple matches
    def exec_objectpath(self,objectpath_query):
        return self._exec_objectpath(objectpath_query,None)

    # Uses ObjectPath to evaluate the given
    # objectpath_query against the current state of the
    # `evaluation_doc_objectpath_tree`
    # Takes a force_return_index_on_multiple_results should multiple matches be found
    # to force the return on a specified element
    def _exec_objectpath(self,objectpath_query,force_return_index_on_multiple_results):
        if self.debug_objectpath_expr:
            logging.debug("exec_objectpath: query: " + objectpath_query)

        qresult = None
        try:
            qresult = self.evaluation_doc_objectpath_tree.execute(objectpath_query)
        except Exception as e:
            if self.debug_objectpath_expr:
                logging.debug("exec_objectpath: query: " + objectpath_query  + " failure: " + str(sys.exc_info()[0]))
                raise e

        if self.debug_objectpath_expr:
            logging.debug("exec_objectpath: query: " + objectpath_query  + " raw result type(): " + str(type(qresult)))

        # Primitive type
        if isinstance(qresult,(str,bool,int)):
            if self.debug_objectpath_expr:
                logging.debug("exec_objectpath: query: " + objectpath_query  + " returning (str|bool|int): " + str(qresult))

            return qresult

        # List or Generator
        elif qresult is not None:
            toreturn = []

            if isinstance(qresult,(list)):
                if len(qresult) > 0:
                    return qresult
                return None

            # assume generator
            else:
                try:
                    while True:
                        r = next(qresult)

                        if self.debug_objectpath_expr:
                            logging.debug("exec_objectpath: query: " + objectpath_query  + " next() returned val: " + str(r))

                        if r is not None:
                            toreturn.append(r)

                except StopIteration as s:
                    if self.debug_objectpath_expr:
                        logging.debug("exec_objectpath: query: " + objectpath_query  + " received StopIteration after " + str(len(toreturn)) + " nexts()..")


            if len(toreturn) == 1:
                toreturn = toreturn[0]
                if self.debug_objectpath_expr:
                    logging.debug("exec_objectpath: query: " + objectpath_query  + " generator had 1 element, returning: " + str(toreturn))

                return toreturn

            elif len(toreturn) > 1:
                if self.debug_objectpath_expr:
                    logging.debug("exec_objectpath: query: " + objectpath_query  + " generator has %d elements: %s" % (len(toreturn),json.dumps(toreturn)))

                # if we are forced to return a specific index on multiple..... do it
                if isinstance(force_return_index_on_multiple_results,(str)):
                    force_return_index_on_multiple_results = int(force_return_index_on_multiple_results)
                if force_return_index_on_multiple_results is not None:
                    toreturn = toreturn[force_return_index_on_multiple_results]
                    if self.debug_objectpath_expr:
                        logging.debug("exec_objectpath: query: " + objectpath_query  + " force_return_index_on_multiple_results=%d , returning val:%s" % (force_return_index_on_multiple_results,str(toreturn)))

                return toreturn

            else:
                return None


        # None...
        else:
            if self.debug_objectpath_expr:
                logging.debug("exec_objectpath: query: " + objectpath_query  + " yielded None")

            return None



class TestsslResultProcessor(object):

    # for controlling access to job_name_2_metrics_db
    lock = threading.RLock()

    result_handler_configs = {}

    # total threads = total amount of commands
    # per file that can be processed concurrently
    threads = 1

    # More debugging
    debug_objectpath_expr = False
    dump_evaldoc_on_error = False
    debug_dump_evaldoc = False

    def dumpEvalDoc(self,evaluation_doc):
        if self.dump_evaldoc_on_error:
            try:
                if evaluation_doc is not None:
                    print("dump_evaldoc_on_error: " + json.dumps(evaluation_doc,indent=2))
                else:
                    print("dump_evaldoc_on_error: evaluation_doc is None!")
            except Exception as etwo:
                logging.exception("Unexpected error attempting dump_evaldoc_on_error")

    # Will process the testssl_json_result_file_path file
    def processResultFile(self,testssl_json_result_file_path,input_dir):

        logging.info("Received event for create of new testssl.sh JSON result file: '%s'", testssl_json_result_file_path)

        # open the file
        testssl_result = None

        # get absolute path & filename optionally
        testssl_json_result_abs_file_path = os.path.abspath(testssl_json_result_file_path)
        testssl_json_result_filename = os.path.basename(testssl_json_result_file_path)

        # init eval doc
        evaluation_doc = None

        # Open the JSON file
        try:
            with open(testssl_json_result_file_path, 'r') as f:
                testssl_result = json.load(f)

                # no scan result
                if 'scanResult' not in testssl_result or len(testssl_result['scanResult']) == 0:
                    logging.info("Result JSON contained empty 'scanResult', skipping: '%s'", testssl_json_result_file_path)
                    return

        except Exception as e:
            logging.exception("Unexpected error in open(): "+testssl_json_result_file_path + " error:" +str(sys.exc_info()[0]))
            raise e

        logging.info("testssl.sh JSON result file loaded OK: '%s'" % testssl_json_result_file_path)

        # for each of our result handler configs
        # lets process the JSON result file through it
        try:
            for config_filename, config in result_handler_configs.items():

                logging.info("Evaluating %s against config '%s' ..." % (testssl_json_result_file_path,config_filename))

                try:
                    # create uberdoc for evaluations
                    evaluation_doc = {
                                config['evaluation_doc_config']['target_keys']['testssl_result_json']: testssl_result,
                                config['evaluation_doc_config']['target_keys']['testssl_result_parent_dir_path']:os.path.dirname(testssl_json_result_file_path).replace(input_dir+"/",""),
                                config['evaluation_doc_config']['target_keys']['testssl_result_parent_dir_abs_path']:os.path.dirname(testssl_json_result_abs_file_path),
                                config['evaluation_doc_config']['target_keys']['testssl_result_file_abs_path']:testssl_json_result_abs_file_path,
                                config['evaluation_doc_config']['target_keys']['testssl_result_filename']:testssl_json_result_filename
                                }

                    # apply any properties found in the path_properties_grok
                    if 'path_properties_grok' in config and config['path_properties_grok'] is not None:
                        grok = Grok(config['path_properties_grok'],custom_patterns=config['custom_groks'])
                        matches = grok.match(testssl_json_result_file_path)

                        # matches?
                        if matches is not None:
                            if 'ignored' in matches:
                                del matches['ignored']
                        else:
                            logging.warn("path_properties_grok: matched nothing! grok:" + config['path_properties_grok'] + " against path: " + testssl_json_result_file_path)
                            matches = {}

                        result_metadata = {
                                          config['evaluation_doc_config']['target_keys']['result_metadata']:matches
                                          }
                        evaluation_doc.update(result_metadata)



                    # Create our Tree to do ObjectPath evals
                    # against our evaluation_doc
                    objectpath_ctx = ObjectPathContext(evaluation_doc,self.debug_objectpath_expr,self.dump_evaldoc_on_error)

                    # for debugging
                    if self.debug_dump_evaldoc:
                        logging.warn("debug_dump_evaldoc: dumping evalution_doc pre cert_expires_objectpath evaluation")
                        self.dumpEvalDoc(evaluation_doc)

                    # Lets grab the cert expires to calc number of days till expiration
                    # Note we force grab the first match...
                    cert_expires_at_str = objectpath_ctx.exec_objectpath(config['cert_expires_objectpath'])
                    cert_expires_at = dateparser.parse(cert_expires_at_str)
                    expires_in_days = cert_expires_at - datetime.datetime.utcnow()
                    evaluation_doc.update({
                                    config['evaluation_doc_config']['target_keys']['cert_expires_in_days']:expires_in_days.days
                                   })


                    # Rebuild our Tree to do ObjectPath evals
                    # against our evaluation_doc to sure the Tree is up to date
                    objectpath_ctx.update(evaluation_doc)

                    # for debugging, dump again as we updated it
                    if self.debug_dump_evaldoc:
                        logging.warn("debug_dump_evaldoc: dumping evalution_doc pre Trigger evaluations")
                        self.dumpEvalDoc(evaluation_doc)

                    # lets process all triggers
                    triggers_fired = []
                    for trigger_name in config['trigger_on']:
                        trigger = config['trigger_on'][trigger_name]
                        objectpath_result = objectpath_ctx.exec_objectpath(trigger['objectpath'])
                        results = []

                        if objectpath_result is not None:

                            # if a primitive....
                            if isinstance(objectpath_result,(str,int,float,bool)):

                                # if a boolean we only include if True....
                                if isinstance(objectpath_result,(bool)):
                                    if objectpath_result is False:
                                        continue

                                results.append(objectpath_result)

                            # if a list ...
                            elif isinstance(objectpath_result,(list)):
                                results = objectpath_result

                            # some other object, throw in a list
                            else:
                                results.append(objectpath_result)

                            # ok we got at least 1 result back
                            # from the objectpath expression
                            if len(results) > 0:
                                triggers_fired.append({
                                                        'tag':trigger_name,
                                                        'title':trigger['title'],
                                                        'reactors':trigger['reactors'],
                                                        'objectpath':trigger['objectpath'],
                                                        'results':results,
                                                        'config_filename':config_filename,
                                                        'testssl_json_result_abs_file_path':testssl_json_result_abs_file_path,
                                                        'testssl_json_result_filename':testssl_json_result_filename,
                                                        'evaluation_doc':evaluation_doc
                                                      })

                    # Triggers were fired
                    # lets process their reactors
                    if len(triggers_fired) > 0:

                        # build a map of reactors -> triggers
                        reactor_triggers = {}
                        for t in triggers_fired:

                            # each trigger can have N reactors
                            for reactor_name in t['reactors']:
                                if reactor_name not in reactor_triggers:
                                    reactor_triggers[reactor_name] = []
                                reactor_triggers[reactor_name].append(t)


                        # for each reactor to invoke...
                        for reactor_name,triggers in reactor_triggers.items():

                            # handle misconfig
                            if reactor_name not in config['reactor_engines']:
                                logging.error("Configured reactor_engine '%s' is not configured?... skipping" % (reactor_name))
                                continue

                            # create the reactor
                            reactor = None
                            reactor_config = config['reactor_engines'][reactor_name]
                            class_name = reactor_config['class_name']
                            try:
                                reactor_class = getattr(importlib.import_module('reactors.' + class_name.lower()), class_name)
                                reactor = reactor_class(reactor_config)
                            except Exception as e:
                                logging.exception("Error loading reactor class: " + class_name + ". Failed to find 'reactors/"+class_name.lower() +".py' with class '"+class_name+"' declared within it")
                                self.dumpEvalDoc(evaluation_doc)
                                raise e

                            # react to the fired triggers
                            logging.debug("Invoking reactor: " + reactor_name + " for " + str(len(triggers)) + " fired triggers")
                            reactor.handleTriggers(triggers,objectpath_ctx)


                except Exception as e:
                    logging.exception("Unexpected error processing: " + testssl_json_result_file_path + " using: " + config_filename + " err:" + str(sys.exc_info()[0]))
                    self.dumpEvalDoc(evaluation_doc)

        except Exception as e:
            logging.exception("Unexpected error processing: " + testssl_json_result_file_path + " " + str(sys.exc_info()[0]))
            self.dumpEvalDoc(evaluation_doc)



class TestsslResultFileMonitor(FileSystemEventHandler):

    # We will feed new input files to this processor
    testssl_result_processor = None

    # max threads
    threads = 1

    # our Pool
    executor = None

    # input_dir_sleep_seconds
    input_dir_sleep_seconds = 0

    # the actual input_dir that we are monitoring
    input_dir = None

    # Regex Filter to match relevent paths in events received
    input_filename_filter = 'testssloutput.+.json'

    def set_threads(self, t):
        self.threads = t

    # to keep track of event.src_paths we have processed
    processed_result_paths = collections.deque(maxlen=400)

    # route to on_modified
    def on_created(self,event):
        super(TestsslResultFileMonitor, self).on_created(event)
        self.on_modified(event)

    # our main impl for processing new json files
    def on_modified(self, event):
        super(TestsslResultFileMonitor, self).on_modified(event)

        if not self.executor:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.threads)

        if event.is_directory:
            return

        # Check if already processed
        if event.src_path in self.processed_result_paths:
            return

        # compile our filter
        input_filename_re_filter = None
        if self.input_filename_filter is not None:
            input_filename_re_filter = re.compile(self.input_filename_filter,re.I)

        if input_filename_re_filter.match(event.src_path):

            # give write time to close....
            time.sleep(self.input_dir_sleep_seconds)

            # file needs data...
            if (os.stat(event.src_path).st_size == 0):
                return

            # Attempt to decode the JSON file
            # if OK then we know its done writing
            try:
                with open(event.src_path, 'r') as f:
                    testssl_result = json.load(f)
                    if testssl_result is None:
                        return

            except json.decoder.JSONDecodeError as e:
                # we just ignore these, it means the file
                # is not done being written
                return

            except Exception as e:
                logging.exception("Unexpected error in open(): "+event.src_path + " error:" +str(sys.exc_info()[0]))
                return

            # Check if already processed
            if event.src_path in self.processed_result_paths:
                return

            logging.info("Responding to parsable testssl.sh JSON result: %s", event.src_path)

            # mark it as processed
            self.processed_result_paths.append(event.src_path)

            # submit for evaluation
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
                        logging.exception(event.src_path + ": Unexpected error in yaml.load("+event.src_path+") " + str(sys.exc_info()[0]))
            except Exception as e:
                logging.exception(event.src_path + ": Unexpected error:" + str(sys.exc_info()[0]))

            # our config name is the filename
            config_filename = os.path.basename(event.src_path)
            result_handler_configs[config_filename] = config


def init_watching(input_dir,
                  config_dir,
                  input_dir_watchdog_threads,
                  input_dir_sleep_seconds,
                  debug_objectpath_expr,
                  input_filename_filter,
                  dump_evaldoc_on_error,
                  debug_dump_evaldoc):

    # mthreaded...
    if (isinstance(input_dir_watchdog_threads,str)):
        input_dir_watchdog_threads = int(input_dir_watchdog_threads)

    # create watchdog to look for new config files
    result_handler_config_monitor = HandlerConfigFileMonitor()

    # create watchdog to look for new files
    event_handler = TestsslResultFileMonitor()
    event_handler.set_threads(input_dir_watchdog_threads)
    event_handler.input_dir = input_dir
    event_handler.input_filename_filter = input_filename_filter
    if (isinstance(input_dir_sleep_seconds,str)):
        input_dir_sleep_seconds = int(input_dir_sleep_seconds)
    event_handler.input_dir_sleep_seconds = input_dir_sleep_seconds

    # Create a TestsslProcessor to consume the testssl_cmds files
    event_handler.testssl_result_processor = TestsslResultProcessor()
    event_handler.testssl_result_processor.debug_objectpath_expr = debug_objectpath_expr
    event_handler.testssl_result_processor.dump_evaldoc_on_error = dump_evaldoc_on_error
    event_handler.testssl_result_processor.debug_dump_evaldoc = debug_dump_evaldoc

    # give the processor the total number of threads to use
    # for processing testssl.sh cmds concurrently
    if (isinstance(input_dir_watchdog_threads,str)):
        input_dir_watchdog_threads = int(input_dir_watchdog_threads)
    event_handler.testssl_result_processor.threads = input_dir_watchdog_threads


    # schedule our config_dir file watchdog
    observer1 = Observer()
    observer1.schedule(result_handler_config_monitor, config_dir, recursive=True)
    observer1.start()

    logging.info("Monitoring for new result handler config YAML files at: %s ",config_dir)

    # lets process any config files already there...
    config_dir_path_to_startup_scan = config_dir
    if config_dir_path_to_startup_scan.startswith("./") or not config_dir_path_to_startup_scan.startswith("/"):
        config_dir_path_to_startup_scan = os.getcwd() + "/" + config_dir_path_to_startup_scan.replace("./","")

    # load any pre existing configs
    for f in os.listdir(config_dir):
        result_handler_config_monitor.on_created(FileCreatedEvent(config_dir + "/" + os.path.basename(f)))

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
    parser.add_argument('-i', '--input-dir', dest='input_dir', default="./input", help="Directory path to recursively monitor for new `*.json` testssl.sh result files. Default './input'")
    parser.add_argument('-f', '--input-filename-filter', dest='input_filename_filter', default=".*testssloutput.+.json", help="Regex for filter --input-dir files from triggering the watchdog. Default '.*testssloutput.+.json'")
    parser.add_argument('-I', '--config-dir', dest='config_dir', default="./configs", help="Directory path to recursively monitor for new `*.yaml` result handler config files. Default './configs'")
    parser.add_argument('-l', '--log-file', dest='log_file', default=None, help="Path to log file, default None, STDOUT")
    parser.add_argument('-x', '--log-level', dest='log_level', default="DEBUG", help="log level, default DEBUG ")
    parser.add_argument('-w', '--input-dir-watchdog-threads', dest='input_dir_watchdog_threads', default=10, help="max threads for watchdog input-dir file processing, default 10")
    parser.add_argument('-s', '--input-dir-sleep-seconds', dest='input_dir_sleep_seconds', default=5, help="When a new *.json file is detected in --input-dir, how many seconds to wait before processing to allow testssl.sh to finish writing. Default 5")
    parser.add_argument('-d', '--debug-object-path-expr', dest='debug_objectpath_expr', default=False, help="Default False. When True, adds more details on ObjectPath expression parsing to logs")
    parser.add_argument('-D', '--debug-dump-evaldoc', action='store_true', help="Flag to enable dumping the 'evaluation_doc' to STDOUT after it is constructed for evaluations (WARNING: this is large & json pretty printed)")
    parser.add_argument('-E', '--dump-evaldoc-on-error', action='store_true', help="Flag to enable dumping the 'evaluation_doc' to STDOUT (json pretty printed) on any error (WARNING: this is large & json pretty printed)")

    args = parser.parse_args()

    logging.basicConfig(level=logging.getLevelName(args.log_level),
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        filename=args.log_file,filemode='w')
    logging.Formatter.converter = time.gmtime

    init_watching(args.input_dir,
                  args.config_dir,
                  args.input_dir_watchdog_threads,
                  int(args.input_dir_sleep_seconds),
                  args.debug_objectpath_expr,
                  args.input_filename_filter,
                  args.dump_evaldoc_on_error,
                  args.debug_dump_evaldoc)
