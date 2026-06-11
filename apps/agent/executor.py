import json
from typing import List, cast
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from genie.core.orchestrator import MemoryOrchestrator
from apps.agent.tools import MEMORY_TOOLS


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
        messages: List[ChatCompletionMessageParam] = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_prompt}]
        tools = cast(List[ChatCompletionToolParam], MEMORY_TOOLS)
        while True:
            response = self.client.chat.completions.create(model=self.model, messages=messages, tools=tools, tool_choice="auto")
            response_message = response.choices[0].message
            assistant_message = response_message.model_dump(exclude_unset=True)
            messages.append(cast(ChatCompletionMessageParam, assistant_message))
            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    if tool_call.type != "function":
                        continue
                    function_name = tool_call.function.name
                    function_to_call = self.available_functions.get(function_name)
                    if not function_to_call:
                        continue
                    function_args = json.loads(tool_call.function.arguments)
                    try:
                        function_response = function_to_call(**function_args)
                        if hasattr(function_response, 'model_dump'):
                            function_response = function_response.model_dump()
                        response_str = json.dumps(function_response)
                    except Exception as e:
                        response_str = f"Error executing tool: {str(e)}"
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
                return response_message.content or "Task completed with no text output."
