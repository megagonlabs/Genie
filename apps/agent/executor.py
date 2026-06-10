import json
import logging
from typing import List, Dict, Any, cast
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from genie.core.orchestrator import MemoryOrchestrator
from apps.agent.tools import MEMORY_TOOLS

logger = logging.getLogger(__name__)


class AgentExecutor:
    def __init__(self, openai_client: OpenAI, orchestrator: MemoryOrchestrator, system_prompt: str):
        self.client = openai_client
        self.orchestrator = orchestrator
        self.system_prompt = system_prompt
        self.model = "gpt-4o"

        self.available_functions = {
            "create_workspace": self.orchestrator.create_workspace,
            "save_memory": self.orchestrator.save_memory,
            "search_memory": self.orchestrator.search_memory,
        }

    def run(self, user_prompt: str) -> str:
        # 1. Initialize strictly typed history
        messages: List[ChatCompletionMessageParam] = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_prompt}]

        # Explicitly cast tools to satisfy Pylance
        tools = cast(List[ChatCompletionToolParam], MEMORY_TOOLS)

        logger.info(f"Starting agent loop for prompt: '{user_prompt}'")

        while True:
            response = self.client.chat.completions.create(model=self.model, messages=messages, tools=tools, tool_choice="auto")

            response_message = response.choices[0].message

            # 2. Convert the OpenAI Message Object to a clean dictionary before appending
            # exclude_unset=True prevents sending null values back to OpenAI which causes API crashes
            assistant_message = response_message.model_dump(exclude_unset=True)
            messages.append(cast(ChatCompletionMessageParam, assistant_message))

            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    # 3. Narrow the type to satisfy Pylance that this is a function call
                    if tool_call.type != "function":
                        continue

                    function_name = tool_call.function.name
                    function_to_call = self.available_functions.get(function_name)

                    if not function_to_call:
                        logger.error(f"LLM hallucinated a function: {function_name}")
                        continue

                    function_args = json.loads(tool_call.function.arguments)
                    logger.info(f"Executing tool: {function_name} with args: {function_args}")

                    try:
                        function_response = function_to_call(**function_args)

                        if hasattr(function_response, 'model_dump'):
                            function_response = function_response.model_dump()

                        response_str = json.dumps(function_response)

                    except Exception as e:
                        logger.error(f"Tool execution failed: {e}")
                        response_str = f"Error executing tool: {str(e)}"

                    # 4. Append the tool result dictionary, cast to the correct type
                    messages.append(
                        cast(
                            ChatCompletionMessageParam,
                            {
                                "tool_call_id": tool_call.id,
                                "role": "tool",
                                "name": function_name,
                                "content": response_str,
                            },
                        )
                    )

            else:
                logger.info("Agent has reached a final conclusion.")
                # We know content is a string here because there are no tool calls
                return response_message.content or "Task completed with no text output."
