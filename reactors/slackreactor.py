__author__ = "bitsofinfo"

from jinja2 import Template, Environment
import time
from slackclient import SlackClient
import json
import logging

import requests

class SlackReactor():

    # our destination...
    webhook_url = None

    # jinja2
    template = None

    # Constructor
    # passed the raw reactor_config object
    def __init__(self, reactor_config):
        self.webhook_url = reactor_config['webhook_url']
        self.template = reactor_config['template']


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

        # Create out standard header text and attachment
        slack_template = env.from_string(self.template)
        rendered_template = slack_template.render(objectpath_ctx.evaluation_doc)

        # Convert to an object we can now append trigger results to
        slack_data = json.loads(rendered_template)

        for t in triggers_fired:

            print(t['testssl_json_result_abs_file_path'])
            print(t['config_filename'])

            attachment_title = t['title']
            attachment_text = "```\n"
            for r in t['results']:
                attachment_text += json.dumps(r)+"\n"
            attachment_text += "```\n"
            slack_data['attachments'].append({'title':attachment_title,'text':attachment_text, 'color':'danger'})


        logging.debug("SlackReactor: Sending to slack....")
        response = requests.post(
            self.webhook_url, data=json.dumps(slack_data),
            headers={'Content-Type': 'application/json'}
        )
        if response.status_code != 200:
            raise ValueError(
                'SlackReactor: Request to slack returned an error %s, the response is:\n%s'
                % (response.status_code, response.text)
            )
