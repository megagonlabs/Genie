import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from pydantic import BaseModel

from apps.agent.executor import AgentExecutor
from genie.core.orchestrator import MemoryOrchestrator


class ToolResultModel(BaseModel):
    node_id: str
    name: str


def make_tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    function = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(id=call_id, type="function", function=function)


def make_completion(*, content=None, tool_calls=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda exclude_unset=True: {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in (tool_calls or [])
            ]
            if tool_calls is not None
            else None,
        },
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TestAgentIntegration:
    @pytest.fixture
    def mock_orchestrator(self):
        orchestrator = MagicMock(spec=MemoryOrchestrator)
        orchestrator.create_workspace.return_value = {
            "node_id": "workspace_123",
            "name": "Quantum Research",
            "parent_id": None,
            "type": "workspace",
        }
        orchestrator.save_memory.return_value = {
            "node_id": "node_456",
            "parent_id": "workspace_123",
            "payload": {"text": "Room-temperature superconductor found."},
            "type": "memory",
        }
        return orchestrator

    @pytest.fixture
    def mock_openai_client(self):
        return MagicMock(spec=OpenAI)

    def test_agent_executor_react_loop_integration(self, mock_openai_client, mock_orchestrator):
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "Quantum Research"}, "call_one")]),
            make_completion(
                tool_calls=[
                    make_tool_call(
                        "save_memory",
                        {
                            "workspace_node_id": "workspace_123",
                            "content_text": "LK-99 retested.",
                            "name": "Superconductor Fact",
                        },
                        "call_two",
                    )
                ]
            ),
            make_completion(content="Research completely structured and finalized.", tool_calls=None),
        ]

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Analyze superconductors.")

        assert mock_openai_client.chat.completions.create.call_count == 3
        mock_orchestrator.create_workspace.assert_called_once_with(name="Quantum Research")
        mock_orchestrator.save_memory.assert_called_once_with(
            workspace_node_id="workspace_123",
            content_text="LK-99 retested.",
            name="Superconductor Fact",
        )
        assert result == "Research completely structured and finalized."

    def test_agent_executor_does_not_rewrite_tool_node_references(self, mock_openai_client, mock_orchestrator):
        mock_orchestrator.save_memory.side_effect = ValueError(
            "workspace_node_id 'Humidity_Effect_on_Photosynthesis' does not exist."
        )
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(
                tool_calls=[
                    make_tool_call(
                        "save_memory",
                        {
                            "workspace_node_id": "Humidity_Effect_on_Photosynthesis",
                            "content_text": "Humidity affects stomatal conductance.",
                            "name": "Humidity Fact",
                        },
                        "call_one",
                    )
                ]
            ),
            make_completion(content="Corrected.", tool_calls=None),
        ]

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Research humidity.")

        assert result == "Corrected."
        mock_orchestrator.save_memory.assert_called_once_with(
            workspace_node_id="Humidity_Effect_on_Photosynthesis",
            content_text="Humidity affects stomatal conductance.",
            name="Humidity Fact",
        )

    def test_agent_executor_serializes_pydantic_tool_results(self, mock_openai_client, mock_orchestrator):
        mock_orchestrator.create_workspace.return_value = ToolResultModel(
            node_id="workspace_999",
            name="Structured Workspace",
        )
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(
                tool_calls=[make_tool_call("create_workspace", {"name": "Structured Workspace"}, "call_one")]
            ),
            make_completion(content="Done.", tool_calls=None),
        ]

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Create a workspace.")

        assert result == "Done."
        second_call_messages = mock_openai_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_call_messages if message["role"] == "tool")
        assert tool_message["role"] == "tool"
        assert json.loads(tool_message["content"]) == {
            "node_id": "workspace_999",
            "name": "Structured Workspace",
        }

    def test_agent_executor_surfaces_tool_errors_back_to_model(self, mock_openai_client, mock_orchestrator):
        mock_orchestrator.search_memory.side_effect = RuntimeError("redis timeout")
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(
                tool_calls=[make_tool_call("search_memory", {"query_text": "gpu roadmap"}, "call_one")]
            ),
            make_completion(content="Recovered.", tool_calls=None),
        ]

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Find GPU roadmap.")

        assert result == "Recovered."
        second_call_messages = mock_openai_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_call_messages if message["role"] == "tool")
        assert tool_message["role"] == "tool"
        assert tool_message["name"] == "search_memory"
        assert tool_message["content"] == "Error executing tool: redis timeout"

    def test_agent_executor_ignores_unknown_tools_and_continues(self, mock_openai_client, mock_orchestrator):
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(
                tool_calls=[make_tool_call("unknown_tool", {"x": 1}, "call_one")]
            ),
            make_completion(content="Finished without using the unknown tool.", tool_calls=None),
        ]

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Do something.")

        assert result == "Finished without using the unknown tool."
        mock_orchestrator.create_workspace.assert_not_called()
        mock_orchestrator.save_memory.assert_not_called()
        mock_orchestrator.search_memory.assert_not_called()

    def test_agent_executor_returns_default_when_model_has_no_text_output(self, mock_openai_client, mock_orchestrator):
        mock_openai_client.chat.completions.create.return_value = make_completion(content=None, tool_calls=None)

        executor = AgentExecutor(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            system_prompt="Test Prompt",
        )
        result = executor.run("Do something.")

        assert result == "Task completed with no text output."
