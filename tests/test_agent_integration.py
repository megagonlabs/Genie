import pytest
from unittest.mock import MagicMock
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from genie.core.orchestrator import MemoryOrchestrator
from apps.agent.executor import AgentExecutor


class TestAgentIntegration:
    @pytest.fixture
    def mock_orchestrator(self):
        orchestrator = MagicMock(spec=MemoryOrchestrator)
        orchestrator.create_workspace.return_value = {"node_id": "workspace_123", "name": "Quantum Research", "parent_id": None, "type": "workspace"}
        orchestrator.save_memory.return_value = {"node_id": "node_456", "parent_id": "workspace_123", "payload": {"text": "Room-temperature superconductor found."}, "type": "memory"}
        return orchestrator

    @pytest.fixture
    def mock_openai_client(self):
        client = MagicMock(spec=OpenAI)
        return client

    def test_agent_executor_react_loop_integration(self, mock_openai_client, mock_orchestrator):
        tool_call_1 = MagicMock()
        tool_call_1.id = "call_one"
        tool_call_1.type = "function"
        tool_call_1.function.name = "create_workspace"
        tool_call_1.function.arguments = '{"name": "Quantum Research"}'
        message_1 = MagicMock(spec=ChatCompletionMessage)
        message_1.content = None
        message_1.tool_calls = [tool_call_1]
        message_1.model_dump.return_value = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_one", "type": "function", "function": {"name": "create_workspace", "arguments": '{"name": "Quantum Research"}'}}],
        }
        choice_1 = MagicMock(spec=Choice)
        choice_1.message = message_1
        completion_1 = MagicMock(spec=ChatCompletion)
        completion_1.choices = [choice_1]
        tool_call_2 = MagicMock()
        tool_call_2.id = "call_two"
        tool_call_2.type = "function"
        tool_call_2.function.name = "save_memory"
        tool_call_2.function.arguments = '{"workspace_node_id": "workspace_123", "content_text": "LK-99 retested.", "name": "Superconductor Fact"}'
        message_2 = MagicMock(spec=ChatCompletionMessage)
        message_2.content = None
        message_2.tool_calls = [tool_call_2]
        message_2.model_dump.return_value = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_two",
                    "type": "function",
                    "function": {"name": "save_memory", "arguments": '{"workspace_node_id": "workspace_123", "content_text": "LK-99 retested.", "name": "Superconductor Fact"}'},
                }
            ],
        }
        choice_2 = MagicMock(spec=Choice)
        choice_2.message = message_2
        completion_2 = MagicMock(spec=ChatCompletion)
        completion_2.choices = [choice_2]
        message_3 = MagicMock(spec=ChatCompletionMessage)
        message_3.content = "Research completely structured and finalized."
        message_3.tool_calls = None
        message_3.model_dump.return_value = {"role": "assistant", "content": "Research completely structured and finalized.", "tool_calls": None}
        choice_3 = MagicMock(spec=Choice)
        choice_3.message = message_3
        completion_3 = MagicMock(spec=ChatCompletion)
        completion_3.choices = [choice_3]
        mock_openai_client.chat.completions.create.side_effect = [completion_1, completion_2, completion_3]
        executor = AgentExecutor(openai_client=mock_openai_client, orchestrator=mock_orchestrator, system_prompt="Test Prompt")
        result = executor.run("Analyze superconductors.")
        assert mock_openai_client.chat.completions.create.call_count == 3
        mock_orchestrator.create_workspace.assert_called_once_with(name="Quantum Research")
        mock_orchestrator.save_memory.assert_called_once_with(workspace_node_id="workspace_123", content_text="LK-99 retested.", name="Superconductor Fact")
        assert result == "Research completely structured and finalized."

    def test_agent_executor_does_not_rewrite_tool_node_references(self, mock_openai_client, mock_orchestrator):
        mock_orchestrator.save_memory.side_effect = ValueError("workspace_node_id 'Humidity_Effect_on_Photosynthesis' does not exist.")
        tool_call_1 = MagicMock()
        tool_call_1.id = "call_one"
        tool_call_1.type = "function"
        tool_call_1.function.name = "save_memory"
        tool_call_1.function.arguments = '{"workspace_node_id": "Humidity_Effect_on_Photosynthesis", "content_text": "Humidity affects stomatal conductance.", "name": "Humidity Fact"}'
        message_1 = MagicMock(spec=ChatCompletionMessage)
        message_1.content = None
        message_1.tool_calls = [tool_call_1]
        message_1.model_dump.return_value = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {
                        "name": "save_memory",
                        "arguments": '{"workspace_node_id": "Humidity_Effect_on_Photosynthesis", "content_text": "Humidity affects stomatal conductance.", "name": "Humidity Fact"}',
                    },
                }
            ],
        }
        choice_1 = MagicMock(spec=Choice)
        choice_1.message = message_1
        completion_1 = MagicMock(spec=ChatCompletion)
        completion_1.choices = [choice_1]
        message_2 = MagicMock(spec=ChatCompletionMessage)
        message_2.content = "Corrected."
        message_2.tool_calls = None
        message_2.model_dump.return_value = {"role": "assistant", "content": "Corrected."}
        choice_2 = MagicMock(spec=Choice)
        choice_2.message = message_2
        completion_2 = MagicMock(spec=ChatCompletion)
        completion_2.choices = [choice_2]
        mock_openai_client.chat.completions.create.side_effect = [completion_1, completion_2]
        executor = AgentExecutor(openai_client=mock_openai_client, orchestrator=mock_orchestrator, system_prompt="Test Prompt")
        result = executor.run("Research humidity.")
        assert result == "Corrected."
        mock_orchestrator.save_memory.assert_called_once_with(
            workspace_node_id="Humidity_Effect_on_Photosynthesis",
            content_text="Humidity affects stomatal conductance.",
            name="Humidity Fact",
        )
