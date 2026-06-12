import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from pydantic import BaseModel

from apps.agent.executor import ToolLoopExecutor
from apps.agent.research_agent import DeepResearchAgent
from apps.agent.tools import MEMORY_TOOLS
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


class TestToolLoopExecutor:
    @pytest.fixture
    def mock_openai_client(self):
        return MagicMock(spec=OpenAI)

    def test_tool_loop_executor_react_loop_integration(self, mock_openai_client):
        available_functions = {
            "create_workspace": MagicMock(return_value={"node_id": "workspace_123"}),
            "save_memory": MagicMock(return_value={"node_id": "node_456"}),
        }
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

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions=available_functions,
        )
        result = executor.run(
            [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Analyze superconductors."}]
        )

        assert mock_openai_client.chat.completions.create.call_count == 3
        available_functions["create_workspace"].assert_called_once_with(name="Quantum Research")
        available_functions["save_memory"].assert_called_once_with(
            workspace_node_id="workspace_123",
            content_text="LK-99 retested.",
            name="Superconductor Fact",
        )
        assert result == "Research completely structured and finalized."

    def test_tool_loop_executor_serializes_pydantic_tool_results(self, mock_openai_client):
        available_functions = {
            "create_workspace": MagicMock(
                return_value=ToolResultModel(node_id="workspace_999", name="Structured Workspace")
            )
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "Structured Workspace"}, "call_one")]),
            make_completion(content="Done.", tool_calls=None),
        ]

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions=available_functions,
        )
        result = executor.run(
            [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Create a workspace."}]
        )

        assert result == "Done."
        second_call_messages = mock_openai_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_call_messages if message["role"] == "tool")
        assert json.loads(tool_message["content"]) == {
            "node_id": "workspace_999",
            "name": "Structured Workspace",
        }

    def test_tool_loop_executor_surfaces_tool_errors_back_to_model(self, mock_openai_client):
        available_functions = {
            "search_memory": MagicMock(side_effect=RuntimeError("redis timeout"))
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("search_memory", {"query_text": "gpu roadmap"}, "call_one")]),
            make_completion(content="Recovered.", tool_calls=None),
        ]

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions=available_functions,
        )
        result = executor.run(
            [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Find GPU roadmap."}]
        )

        assert result == "Recovered."
        second_call_messages = mock_openai_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_call_messages if message["role"] == "tool")
        assert tool_message["name"] == "search_memory"
        assert tool_message["content"] == "Error executing tool: redis timeout"

    def test_tool_loop_executor_ignores_unknown_tools_and_continues(self, mock_openai_client):
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("unknown_tool", {"x": 1}, "call_one")]),
            make_completion(content="Finished without using the unknown tool.", tool_calls=None),
        ]

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions={},
        )
        result = executor.run(
            [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}]
        )

        assert result == "Finished without using the unknown tool."

    def test_tool_loop_executor_returns_default_when_model_has_no_text_output(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value = make_completion(content=None, tool_calls=None)

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions={},
        )
        result = executor.run(
            [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}]
        )

        assert result == "Task completed with no text output."

    def test_tool_loop_executor_rejects_endless_loop(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value = make_completion(
            tool_calls=[make_tool_call("unknown_tool", {"x": 1}, "call_one")]
        )

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions={},
            max_round_trips=2,
        )

        with pytest.raises(RuntimeError, match="maximum of 2 assistant turns"):
            executor.run(
                [{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}]
            )


class TestDeepResearchAgent:
    @pytest.fixture
    def mock_orchestrator(self):
        orchestrator = MagicMock(spec=MemoryOrchestrator)
        orchestrator.create_workspace.return_value = {"node_id": "workspace_123"}
        orchestrator.save_memory.return_value = {"node_id": "node_456"}
        orchestrator.search_memory.return_value = [{"node_id": "node_1", "text": "saved fact"}]
        return orchestrator

    @pytest.fixture
    def mock_openai_client(self):
        return MagicMock(spec=OpenAI)

    def test_deep_research_agent_runs_plan_execute_synthesize_workflow(
        self,
        mock_openai_client,
        mock_orchestrator,
    ):
        planning_payload = {
            "topic_title": "Quantum Research",
            "goal": "Research the topic.",
            "report_spec": {
                "audience": "executive team",
                "deliverable": "comprehensive report",
                "success_criteria": ["structured findings"],
            },
            "workspace_tree": [{"name": "quantum_research", "children": ["evidence"]}],
            "subtasks": [
                {
                    "id": "task_1",
                    "title": "Collect findings",
                    "objective": "Gather evidence",
                    "workspace": "evidence",
                    "questions": ["What happened?"],
                    "deliverables": ["saved notes"],
                }
            ],
            "report_outline": [{"section": "Summary", "purpose": "overview"}],
            "final_search_queries": ["quantum findings"],
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(content=json.dumps(planning_payload), tool_calls=None),
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "quantum_research"}, "call_one")]),
            make_completion(
                tool_calls=[
                    make_tool_call(
                        "save_memory",
                        {
                            "workspace_node_id": "workspace_123",
                            "content_text": "A saved finding.",
                            "name": "finding_1",
                        },
                        "call_two",
                    )
                ]
            ),
            make_completion(content="Execution finished with structured notes.", tool_calls=None),
            make_completion(content="Final comprehensive report.", tool_calls=None),
        ]

        agent = DeepResearchAgent(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
        )
        result = agent.run("Research quantum computing.")

        assert result == "Final comprehensive report."
        mock_orchestrator.create_workspace.assert_called_once_with(name="quantum_research")
        mock_orchestrator.save_memory.assert_called_once_with(
            workspace_node_id="workspace_123",
            content_text="A saved finding.",
            name="finding_1",
        )
        mock_orchestrator.search_memory.assert_called_once_with(
            query_text="quantum findings",
            top_k=8,
        )
        assert mock_openai_client.chat.completions.create.call_count == 5

    def test_deep_research_agent_skips_invalid_final_queries(self, mock_openai_client, mock_orchestrator):
        planning_payload = {
            "topic_title": "Topic",
            "goal": "Goal",
            "report_spec": {"audience": "team", "deliverable": "report", "success_criteria": ["done"]},
            "workspace_tree": [],
            "subtasks": [],
            "report_outline": [],
            "final_search_queries": ["valid query", "", None, 7],
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(content=json.dumps(planning_payload), tool_calls=None),
            make_completion(content="Execution finished.", tool_calls=None),
            make_completion(content="Final report.", tool_calls=None),
        ]

        agent = DeepResearchAgent(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            max_tool_round_trips=1,
        )
        result = agent.run("Research topic.")

        assert result == "Final report."
        mock_orchestrator.search_memory.assert_called_once_with(
            query_text="valid query",
            top_k=8,
        )
