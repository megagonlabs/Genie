"""Reusable chat-completions tool executor for agent workflows."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Mapping, Tuple, cast

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from apps.agent.models import ToolCallTrace, ToolLoopResult

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
        return self.run_detailed(messages).final_output

    def run_detailed(self, messages: List[ChatCompletionMessageParam]) -> ToolLoopResult:
        """Run the assistant/tool loop and return the full execution trace.

        Parameters:
        messages:
            Initial chat history to send to the model.

        Returns:
        ToolLoopResult
            Final assistant output together with a per-tool execution trace.
        """
        trace: List[ToolCallTrace] = []

        for round_index in range(self.max_round_trips):
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
                return ToolLoopResult(
                    final_output=response_message.content or "Task completed with no text output.",
                    tool_calls=trace,
                    assistant_turn_count=round_index + 1,
                )

            for tool_call in response_message.tool_calls:
                if tool_call.type != "function":
                    continue

                function_name = tool_call.function.name
                function_to_call = self.available_functions.get(function_name)
                step_number = len(trace) + 1
                if function_to_call is None:
                    response_str = f"Error executing tool: Unknown tool '{function_name}'."
                    trace.append(
                        ToolCallTrace(
                            step_number=step_number,
                            tool_call_id=tool_call.id,
                            tool_name=function_name,
                            arguments=self._parse_tool_arguments(tool_call.function.arguments),
                            result=None,
                            response_text=response_str,
                            succeeded=False,
                            error_message=f"Unknown tool '{function_name}'.",
                        )
                    )
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
                    continue

                trace_entry, response_str = self._execute_tool_call(
                    function_name=function_name,
                    function_to_call=function_to_call,
                    raw_arguments=tool_call.function.arguments,
                    tool_call_id=tool_call.id,
                    step_number=step_number,
                )
                trace.append(trace_entry)
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

    def _parse_tool_arguments(self, raw_arguments: str) -> Dict[str, Any]:
        """Parse JSON tool arguments into a dictionary when possible."""
        try:
            function_args = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return function_args if isinstance(function_args, dict) else {}

    def _execute_tool_call(
        self,
        *,
        function_name: str,
        function_to_call: ToolCallable,
        raw_arguments: str,
        tool_call_id: str,
        step_number: int,
    ) -> Tuple[ToolCallTrace, str]:
        """Execute a single tool call and capture both its trace and response text."""
        try:
            function_args = json.loads(raw_arguments)
            if not isinstance(function_args, dict):
                raise TypeError("Tool arguments must decode to a JSON object.")
        except (json.JSONDecodeError, TypeError) as exc:
            response_text = f"Error executing tool: Invalid JSON arguments ({exc})."
            return (
                ToolCallTrace(
                    step_number=step_number,
                    tool_call_id=tool_call_id,
                    tool_name=function_name,
                    arguments={},
                    result=None,
                    response_text=response_text,
                    succeeded=False,
                    error_message=f"Invalid JSON arguments ({exc}).",
                ),
                response_text,
            )

        try:
            function_response = function_to_call(**function_args)
            if hasattr(function_response, "model_dump"):
                function_response = function_response.model_dump()
            response_text = json.dumps(function_response, default=str)
            returned_node_id = None
            if isinstance(function_response, dict):
                returned_node_id_value = function_response.get("node_id")
                if isinstance(returned_node_id_value, str):
                    returned_node_id = returned_node_id_value
            return (
                ToolCallTrace(
                    step_number=step_number,
                    tool_call_id=tool_call_id,
                    tool_name=function_name,
                    arguments=function_args,
                    result=function_response,
                    response_text=response_text,
                    succeeded=True,
                    returned_node_id=returned_node_id,
                ),
                response_text,
            )
        except Exception as exc:  # pragma: no cover - behavior validated indirectly via app flow.
            response_text = f"Error executing tool: {str(exc)}"
            return (
                ToolCallTrace(
                    step_number=step_number,
                    tool_call_id=tool_call_id,
                    tool_name=function_name,
                    arguments=function_args,
                    result=None,
                    response_text=response_text,
                    succeeded=False,
                    error_message=str(exc),
                ),
                response_text,
            )
