"""Reusable chat-completions tool executor for agent workflows."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, List, Mapping, cast

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

ToolCallable = Callable[..., Any]


class ToolLoopExecutor:
    """Execute an iterative tool-calling loop with the Chat Completions API.

    Parameters:
    openai_client:
        OpenAI client used to send chat completion requests.
    model:
        Model identifier used for all loop turns.
    tools:
        Tool schema definitions exposed to the model.
    available_functions:
        Mapping of tool names to local callables.
    max_round_trips:
        Safety limit for the number of assistant turns before aborting.
    """

    def __init__(
        self,
        openai_client: OpenAI,
        model: str,
        tools: Iterable[Mapping[str, Any]],
        available_functions: Mapping[str, ToolCallable],
        max_round_trips: int = 20,
    ) -> None:
        self.client = openai_client
        self.model = model
        self.tools = cast(List[ChatCompletionToolParam], list(tools))
        self.available_functions = dict(available_functions)
        self.max_round_trips = max_round_trips

    def run(self, messages: List[ChatCompletionMessageParam]) -> str:
        """Run the assistant/tool loop until the model returns a final answer.

        Parameters:
        messages:
            Initial chat history to send to the model.

        Returns:
        str
            The final assistant response content.
        """
        for _ in range(self.max_round_trips):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
            )
            response_message = response.choices[0].message
            assistant_message = response_message.model_dump(exclude_unset=True)
            messages.append(cast(ChatCompletionMessageParam, assistant_message))

            if not response_message.tool_calls:
                return response_message.content or "Task completed with no text output."

            for tool_call in response_message.tool_calls:
                if tool_call.type != "function":
                    continue

                function_name = tool_call.function.name
                function_to_call = self.available_functions.get(function_name)
                if function_to_call is None:
                    messages.append(
                        cast(
                            ChatCompletionMessageParam,
                            {
                                "tool_call_id": tool_call.id,
                                "role": "tool",
                                "name": function_name,
                                "content": f"Error executing tool: Unknown tool '{function_name}'.",
                            },
                        )
                    )
                    continue

                response_str = self._execute_tool_call(function_to_call, tool_call.function.arguments)
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

        raise RuntimeError(
            f"Tool loop exceeded the maximum of {self.max_round_trips} assistant turns."
        )

    def _execute_tool_call(self, function_to_call: ToolCallable, raw_arguments: str) -> str:
        """Execute a single tool call and convert the result into JSON text."""
        try:
            function_args = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return f"Error executing tool: Invalid JSON arguments ({exc})."

        try:
            function_response = function_to_call(**function_args)
            if hasattr(function_response, "model_dump"):
                function_response = function_response.model_dump()
            return json.dumps(function_response)
        except Exception as exc:  # pragma: no cover - behavior validated indirectly via app flow.
            return f"Error executing tool: {str(exc)}"
