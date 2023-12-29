import asyncio
import uvloop
import time
import os
from twilio.rest import Client
import requests
import tiktoken
from bolna.agent_manager import TaskManager
from bolna.helpers.logger_config import configure_logger

# Find your Account SID and Auth Token at twilio.com/console
# and set the environment variables. See http://twil.io/secure
account_sid = os.environ['TWILIO_ACCOUNT_SID']
auth_token = os.environ['TWILIO_AUTH_TOKEN']
client = Client(account_sid, auth_token)
enc = tiktoken.get_encoding("cl100k_base")

logger = configure_logger(__name__)


class AssistantManager:
    def __init__(self, agent_config, ws, context_data=None, user_id=None, assistant_id=None, connected_through_dashboard = None):
        # Set up communication queues between processes
        self.tools = {}
        self.websocket = ws
        self.agent_config = agent_config
        self.context_data = context_data
        self.tasks = agent_config.get('tasks', [])
        self.task_states = [False] * len(self.tasks)
        self.user_id = user_id
        self.assistant_id = assistant_id
        self.run_id = f"{self.assistant_id}#{str(int(time.time() * 1000))}"  # multiply by 1000 to get timestamp in nano seconds to reduce probability of collisions in simultaneously triggered runs.
        self.connected_through_dashboard = connected_through_dashboard
        
    @staticmethod
    def find_llm_output_price(outputs):
        num_token = 0
        for op in outputs:
            num_token += len(enc.encode(str(op)))
        return 0.0020 * num_token

    @staticmethod
    def find_llm_input_token_price(messages):
        total_str = []
        this_run = ''
        prev_run = ''
        num_token = 0
        for message in messages:
            if message['role'] == 'system':
                this_run += message['content']

            if message['role'] == 'user':
                this_run += message['content']

            if message['role'] == 'assistant':
                num_token += len(enc.encode(str(this_run)))
                this_run += message['content']

        return 0.0010 * num_token

    async def _save_meta(self, call_sid, stream_sid, messages, transcriber_characters, synthesizer_characters,
                         label_flow):
        logger.info(f"call sid {call_sid}, stream_sid {stream_sid}")
        # transcriber_cost = time * 0.0043/ 60
        # telephony_cost = cost
        # llm_cost = input_tokens  * price + output_tokens * price
        # tts_cost = 0 for now
        # if polly - characters * 16/1000000
        #     input_tokens, output_tokens

        call_meta = dict()
        call = client.calls(call_sid).fetch()
        call_meta["telephony_cost"] = call.price
        call_meta["duration"] = call.duration
        call_meta["transcriber_cost"] = int(call.duration) * (0.0043 / 60)
        call_meta["to_number"] = call.to_formatted
        recording = client.recordings.list(call_sid=call_sid)[0]
        call_meta["recording_url"] = recordings.media_url
        call_meta["tts_cost"] = 0 if self.tasks[0]['tools_config']['synthesizer']['model'] != "polly" else (
                    synthesizer_characters * 16 / 1000000)
        call_meta["llm_cost"] = self.find_llm_input_token_price(messages) + self.find_llm_output_token_price(label_flow)
        logger.info(f"Saving call meta {call_meta}")
        await self.dynamodb.store_run(self.user_id, self.assistant_id, self.run_id, call_meta)

    async def download_record_from_twilio_and_save_to_s3(self, recording_url):
        response = requests.get(recording_url, auth=(account_sid, auth_token))
        if response.status_code == 200:
            bucket_name = 'bolna/'
            object_key = 'user_id/agent_id/run_id.mp3'

            # Upload the downloaded MP3 file to S3
            s3.put_object(Bucket=bucket_name, Key=object_key, Body=response.content)
            print("MP3 file uploaded to S3 successfully!")

    async def run(self, is_local=False):
        '''
        Run will start all tasks in sequential format
        '''
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        input_parameters = None
        for task_id, task in enumerate(self.tasks):
            task_manager = TaskManager(self.agent_config["assistant_name"], task_id, task, self.websocket,
                                       context_data=self.context_data, input_parameters=input_parameters,
                                       user_id=self.user_id, assistant_id=self.assistant_id, run_id=self.run_id, connected_through_dashboard = self.connected_through_dashboard)
            await task_manager.load_prompt(self.agent_config["assistant_name"], task_id, is_local=is_local)
            task_output = await task_manager.run()
            task_output['run_id'] = self.run_id
            yield (task_id, task_output)
            logger.info(f"Got new parameters {task_output}")
            self.task_states[task_id] = True
            input_parameters = task_output
        logger.info("Done with execution of the agent")