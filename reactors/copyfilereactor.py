__author__ = "bitsofinfo"

from jinja2 import Template, Environment
import time
import json
import logging
import pathlib
import shutil
import os

import requests

class CopyFileReactor():

    # our source...
    copy_from = None

    # our destination...
    copy_to = None

    # cleanup config
    cleanup = None

    # Constructor
    # passed the raw reactor_config object
    def __init__(self, reactor_config):
        self.copy_from = reactor_config['copy_from']
        self.copy_to = reactor_config['copy_to']

        if 'cleanup' in reactor_config:
            self.cleanup = reactor_config['cleanup']


    # When invoked this is passed
    #
    # - 'triggers_fired' - array of trigger_result objects.
    #                      Where each trigge_result is defined as:
    # {
    #    'tag': [short name of the trigger]
    #    'title':[see above, title of the trigger name],
    #    'reactors':[see above, array of configured reactor names],
    #    'objectpath':[see above, the objectpath],
    #    'results':[array of raw object path result values],
    #    'config_filename':[name of the YAML config the trigger was defined in],
    #    'testssl_json_result_abs_file_path':[absolute path to the testssl.sh JSON result file],
    #    'testssl_json_result_filename':[filename only of JSON result file],
    #    'evaluation_doc':[the evalution_doc object that the trigger evaluated]
    #  }
    #
    #
    # - 'objectpath_ctx' - a reference to the ObjectPathContext object used
    #                      when processing the trigger evaluations. The following
    #                      ObjectPathContext properties can be used in the reactor
    #                      for further ObjectPath based functionality if the reactor
    #                      plugin wishes to take advantage of it.
    # {
    #    exec_objectpath: ObjectPathContext function reference
    #    exec_objectpath_specific_match: ObjectPathContext function reference
    #    exec_objectpath_first_match: ObjectPathContext function reference
    #    evaluation_doc: the raw evalution_doc object that the trigger evaluated
    #  }
    #
    #
    def handleTriggers(self, triggers_fired, objectpath_ctx):

        # Create an Jinja2 Environment
        # and register a new filter for the exec_objectpath methods
        env = Environment()
        env.filters['exec_objectpath'] = objectpath_ctx.exec_objectpath
        env.filters['exec_objectpath_specific_match'] = objectpath_ctx.exec_objectpath_specific_match
        env.filters['exec_objectpath_first_match'] = objectpath_ctx.exec_objectpath_first_match

        # optionally handle cleanup if configured
        if self.cleanup is not None:
            logging.debug("CopyFileReactor: attempting cleanup of " + self.cleanup['path'] + " older than " + str(self.cleanup['delete_older_than_days']) + " days...")
            now = time.time() # in seconds!
            purge_older_than = now - (float(self.cleanup['delete_older_than_days']) * 86400) # 86400 = 24 hours = 1 day
            for root, dirs, files in os.walk(self.cleanup['path'], topdown=False):
                for _dir in dirs:
                    toeval = root+"/"+_dir
                    dir_timestamp = os.path.getmtime(toeval)
                    if dir_timestamp < purge_older_than:
                        logging.debug("CopyFileReactor: cleanup: removing old directory: " +toeval)
                        shutil.rmtree(toeval)

        for t in triggers_fired:

            try:
                # FROM
                copy_from_template = env.from_string(self.copy_from)
                rendered_copy_from = copy_from_template.render(t)

                # TO
                copy_to_template = env.from_string(self.copy_to)
                rendered_copy_to = copy_to_template.render(t)

                if not os.path.isfile(rendered_copy_from):
                    logging.error("skipping: trigger['"+t['title']+"'] copy_from: yielded a non-existant path: " + rendered_copy_from)
                    continue


                # make all target directories
                pathlib.Path(rendered_copy_to).mkdir(parents=True, exist_ok=True)

                # execute the copy
                shutil.copy(rendered_copy_from,rendered_copy_to)

                logging.info("CopyFileReactor: Copied OK " + rendered_copy_from + " TO " + rendered_copy_to)

            except Exception as e:
                logging.exception("CopyFileReactor: Error copying " + rendered_copy_from + " TO " + rendered_copy_to)
