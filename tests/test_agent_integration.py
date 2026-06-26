import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from openai import OpenAI
from pydantic import BaseModel

from apps.agent.executor import ToolLoopExecutor
from apps.agent.models import ProgressEvent
from apps.agent.research_agent import DeepResearchAgent
from apps.agent.tools import MEMORY_TOOLS
from genie.core.orchestrator import MemoryOrchestrator
from genie.integrations.agent_tools import (
    DEFAULT_MEMORY_TOOL_NAMES,
    build_memory_tool_registry,
    get_memory_tool_schemas,
)


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
            "tool_calls": (
                [
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
                else None
            ),
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
        result = executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Analyze superconductors."}])

        assert mock_openai_client.chat.completions.create.call_count == 3
        available_functions["create_workspace"].assert_called_once_with(name="Quantum Research")
        available_functions["save_memory"].assert_called_once_with(
            workspace_node_id="workspace_123",
            content_text="LK-99 retested.",
            name="Superconductor Fact",
        )
        assert result == "Research completely structured and finalized."

    def test_tool_loop_executor_run_detailed_captures_trace(self, mock_openai_client):
        available_functions = {
            "create_workspace": MagicMock(return_value={"node_id": "workspace_123"}),
            "search_memory": MagicMock(return_value=[{"node_id": "node_456"}]),
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "Quantum Research"}, "call_one")]),
            make_completion(tool_calls=[make_tool_call("search_memory", {"query_text": "superconductors"}, "call_two")]),
            make_completion(content="Done tracing.", tool_calls=None),
        ]

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions=available_functions,
        )
        result = executor.run_detailed([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Analyze superconductors."}])

        assert result.final_output == "Done tracing."
        assert result.assistant_turn_count == 3
        assert [trace.tool_name for trace in result.tool_calls] == ["create_workspace", "search_memory"]
        assert result.tool_calls[0].returned_node_id == "workspace_123"
        assert result.tool_calls[1].result == [{"node_id": "node_456"}]

    def test_tool_loop_executor_emits_progress_events(self, mock_openai_client):
        available_functions = {
            "create_workspace": MagicMock(return_value={"node_id": "workspace_123"}),
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "Quantum Research"}, "call_one")]),
            make_completion(content="Done tracing.", tool_calls=None),
        ]
        progress_events: list[ProgressEvent] = []

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions=available_functions,
            on_progress=progress_events.append,
        )
        executor.run_detailed([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Analyze superconductors."}])

        assert any(event.stage == "execution" and event.status == "started" for event in progress_events)
        assert any(event.tool_name == "create_workspace" and event.step_number == 1 for event in progress_events)
        assert any(event.stage == "execution" and event.status == "completed" and "final assistant response" in event.message.lower() for event in progress_events)

    def test_tool_loop_executor_serializes_pydantic_tool_results(self, mock_openai_client):
        available_functions = {"create_workspace": MagicMock(return_value=ToolResultModel(node_id="workspace_999", name="Structured Workspace"))}
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
        result = executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Create a workspace."}])

        assert result == "Done."
        second_call_messages = mock_openai_client.chat.completions.create.call_args_list[1].kwargs["messages"]
        tool_message = next(message for message in second_call_messages if message["role"] == "tool")
        assert json.loads(tool_message["content"]) == {
            "node_id": "workspace_999",
            "name": "Structured Workspace",
        }

    def test_tool_loop_executor_surfaces_tool_errors_back_to_model(self, mock_openai_client):
        available_functions = {"search_memory": MagicMock(side_effect=RuntimeError("redis timeout"))}
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
        result = executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Find GPU roadmap."}])

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
        result = executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}])

        assert result == "Finished without using the unknown tool."

    def test_tool_loop_executor_returns_default_when_model_has_no_text_output(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value = make_completion(content=None, tool_calls=None)

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions={},
        )
        result = executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}])

        assert result == "Task completed with no text output."

    def test_tool_loop_executor_rejects_endless_loop(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value = make_completion(tool_calls=[make_tool_call("unknown_tool", {"x": 1}, "call_one")])

        executor = ToolLoopExecutor(
            openai_client=mock_openai_client,
            model="gpt-4o",
            tools=MEMORY_TOOLS,
            available_functions={},
            max_round_trips=2,
        )

        with pytest.raises(RuntimeError, match="maximum of 2 assistant turns"):
            executor.run([{"role": "system", "content": "Test Prompt"}, {"role": "user", "content": "Do something."}])


class TestGenieAgentTooling:
    def test_get_memory_tool_schemas_returns_default_reusable_set(self):
        schemas = get_memory_tool_schemas()

        assert [schema["function"]["name"] for schema in schemas] == list(DEFAULT_MEMORY_TOOL_NAMES)
        assert schemas is not MEMORY_TOOLS
        assert [schema["function"]["name"] for schema in MEMORY_TOOLS] == list(DEFAULT_MEMORY_TOOL_NAMES)

    def test_get_memory_tool_schemas_supports_ordered_subset(self):
        schemas = get_memory_tool_schemas(["search_memory", "create_workspace"])

        assert [schema["function"]["name"] for schema in schemas] == [
            "search_memory",
            "create_workspace",
        ]

    def test_build_memory_tool_registry_uses_selected_subset(self):
        orchestrator = MagicMock(spec=MemoryOrchestrator)

        registry = build_memory_tool_registry(
            orchestrator,
            tool_names=["search_memory", "create_workspace"],
        )

        assert list(registry) == ["search_memory", "create_workspace"]
        assert registry["search_memory"] is orchestrator.search_memory
        assert registry["create_workspace"] is orchestrator.create_workspace

    def test_build_memory_tool_registry_rejects_unknown_tool_name(self):
        orchestrator = MagicMock(spec=MemoryOrchestrator)

        with pytest.raises(ValueError, match="Unknown Genie tool name"):
            build_memory_tool_registry(orchestrator, tool_names=["unknown_tool"])


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
            scope_node_id=None,
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
            scope_node_id=None,
            top_k=8,
        )

    def test_deep_research_agent_run_with_audit_returns_verification_bundle(
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
            make_completion(
                tool_calls=[
                    make_tool_call(
                        "create_workspace",
                        {"name": "quantum_research", "parent_id": "workspace_research_root"},
                        "call_one",
                    )
                ]
            ),
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
            make_completion(
                tool_calls=[
                    make_tool_call(
                        "search_memory",
                        {
                            "query_text": "quantum findings",
                            "scope_node_id": "workspace_research_root",
                            "top_k": 3,
                        },
                        "call_three",
                    )
                ]
            ),
            make_completion(content="Execution finished with structured notes.", tool_calls=None),
            make_completion(content="Final comprehensive report.", tool_calls=None),
        ]
        mock_orchestrator.search_memory.side_effect = [
            [{"node_id": "node_456", "text": "saved fact"}],
            [{"node_id": "node_456", "text": "saved fact"}],
        ]

        agent = DeepResearchAgent(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
        )
        agent._initialize_run_tree = MagicMock(
            return_value={
                "audit_root_id": "workspace_run_root",
                "research_root_id": "workspace_research_root",
                "audit_workspace_id": "workspace_audit",
                "plan_workspace_id": "workspace_plan",
                "execution_workspace_id": "workspace_execution",
                "recall_workspace_id": "workspace_recall",
                "report_workspace_id": "workspace_report",
                "verification_workspace_id": "workspace_verification",
            }
        )
        agent._persist_artifact = MagicMock(
            side_effect=[
                "node_plan",
                "node_execution",
                "node_final_report",
                "node_verification",
            ]
        )
        agent._persist_recall_artifacts = MagicMock(return_value=(["node_recall"], ["node_456"]))

        result = agent.run_with_audit("Research quantum computing.")

        assert result.final_report == "Final comprehensive report."
        assert result.research_root_node_id == "workspace_research_root"
        assert result.verification.status == "warn"
        assert result.verification.metrics["memory_write_count"] == 1
        assert result.verification.metrics["overlap_node_ids"] == ["node_456"]
        assert any(check.name == "recalled_memories_overlap_saved_work" and check.status == "pass" for check in result.verification.checks)
        assert "saving more intermediate notes would strengthen provenance" in " ".join(result.verification.warnings).lower()
        assert mock_orchestrator.search_memory.call_args_list == [
            call(query_text="quantum findings", scope_node_id="workspace_research_root", top_k=3),
            call(query_text="quantum findings", scope_node_id="workspace_research_root", top_k=8),
        ]

    def test_deep_research_agent_verification_flags_single_shot_like_runs(
        self,
        mock_openai_client,
        mock_orchestrator,
    ):
        planning_payload = {
            "topic_title": "Humidity Research",
            "goal": "Research the topic.",
            "report_spec": {
                "audience": "growers",
                "deliverable": "report",
                "success_criteria": ["useful"],
            },
            "workspace_tree": [],
            "subtasks": [
                {
                    "id": "task_1",
                    "title": "Direct answer",
                    "objective": "Answer immediately",
                    "workspace": "root",
                    "questions": ["What is the answer?"],
                    "deliverables": ["report"],
                }
            ],
            "report_outline": [],
            "final_search_queries": [],
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(content=json.dumps(planning_payload), tool_calls=None),
            make_completion(content="Execution finished immediately.", tool_calls=None),
            make_completion(content="Final report.", tool_calls=None),
        ]

        agent = DeepResearchAgent(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            max_tool_round_trips=1,
        )
        agent._initialize_run_tree = MagicMock(
            return_value={
                "audit_root_id": "workspace_run_root",
                "research_root_id": "workspace_research_root",
                "audit_workspace_id": "workspace_audit",
                "plan_workspace_id": "workspace_plan",
                "execution_workspace_id": "workspace_execution",
                "recall_workspace_id": "workspace_recall",
                "report_workspace_id": "workspace_report",
                "verification_workspace_id": "workspace_verification",
            }
        )
        agent._persist_artifact = MagicMock(return_value=None)
        agent._persist_recall_artifacts = MagicMock(return_value=([], []))

        result = agent.run_with_audit("Research humidity and photosynthesis.")

        assert result.verification.status == "fail"
        assert any(check.name == "intermediate_memories_saved" and check.status == "fail" for check in result.verification.checks)

    def test_deep_research_agent_emits_lifecycle_progress_events(
        self,
        mock_openai_client,
        mock_orchestrator,
    ):
        planning_payload = {
            "topic_title": "Quantum Research",
            "goal": "Research the topic.",
            "report_spec": {"audience": "team", "deliverable": "report", "success_criteria": ["done"]},
            "workspace_tree": [],
            "subtasks": [],
            "report_outline": [],
            "final_search_queries": ["quantum findings"],
        }
        mock_openai_client.chat.completions.create.side_effect = [
            make_completion(content=json.dumps(planning_payload), tool_calls=None),
            make_completion(tool_calls=[make_tool_call("create_workspace", {"name": "quantum_research"}, "call_one")]),
            make_completion(content="Execution finished.", tool_calls=None),
            make_completion(content="Final report.", tool_calls=None),
        ]
        mock_orchestrator.search_memory.return_value = [{"node_id": "node_1", "text": "saved fact"}]
        progress_events: list[ProgressEvent] = []

        agent = DeepResearchAgent(
            openai_client=mock_openai_client,
            orchestrator=mock_orchestrator,
            on_progress=progress_events.append,
        )
        agent._initialize_run_tree = MagicMock(
            return_value={
                "audit_root_id": "workspace_run_root",
                "research_root_id": "workspace_research_root",
                "audit_workspace_id": "workspace_audit",
                "plan_workspace_id": "workspace_plan",
                "execution_workspace_id": "workspace_execution",
                "recall_workspace_id": "workspace_recall",
                "report_workspace_id": "workspace_report",
                "verification_workspace_id": "workspace_verification",
            }
        )
        agent._persist_artifact = MagicMock(return_value=None)
        agent._persist_recall_artifacts = MagicMock(return_value=([], ["node_1"]))

        result = agent.run_with_audit("Research quantum computing.")

        assert result.final_report == "Final report."
        observed_stages = {(event.stage, event.status) for event in progress_events}
        assert ("run", "started") in observed_stages
        assert ("planning", "started") in observed_stages
        assert ("planning", "completed") in observed_stages
        assert ("execution", "started") in observed_stages
        assert ("execution", "completed") in observed_stages
        assert ("recall", "started") in observed_stages
        assert ("recall", "completed") in observed_stages
        assert ("synthesis", "started") in observed_stages
        assert ("synthesis", "completed") in observed_stages
        assert ("verification", "started") in observed_stages
        assert ("verification", "completed") in observed_stages
        assert ("run", "completed") in observed_stages
        assert any(event.tool_name == "create_workspace" for event in progress_events)
