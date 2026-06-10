import pytest
from unittest.mock import MagicMock, patch
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from genie.core.orchestrator import MemoryOrchestrator
from apps.agent.executor import AgentExecutor


class TestAgentIntegration:
    @pytest.fixture
    def mock_orchestrator(self):
        """Creates a mock orchestrator to capture tool execution calls."""
        orchestrator = MagicMock(spec=MemoryOrchestrator)
        # Mock create_workspace to return a structural node dictionary
        orchestrator.create_workspace.return_value = {"node_id": "workspace_123", "name": "Quantum Research", "parent_id": None, "type": "workspace"}
        # Mock save_memory to return a memory node dictionary
        orchestrator.save_memory.return_value = {"node_id": "node_456", "parent_id": "workspace_123", "payload": {"text": "Room-temperature superconductor found."}, "type": "memory"}
        return orchestrator

    @pytest.fixture
    def mock_openai_client(self):
        """Creates a mock OpenAI client to simulate consecutive API turns."""
        client = MagicMock(spec=OpenAI)
        return client

    def test_agent_executor_react_loop_integration(self, mock_openai_client, mock_orchestrator):
        """
        Tests the full ReAct loop end-to-end.
        Simulates: Tool Call (Workspace) -> Tool Call (Save) -> Final Text Answer.
        """
        # --- Turn 1: LLM wants to create a workspace ---
        tool_call_1 = MagicMock()
        tool_call_1.id = "call_one"
        tool_call_1.type = "function"
        tool_call_1.function.name = "create_workspace"
        tool_call_1.function.arguments = '{"name": "Quantum Research"}'

        message_1 = MagicMock(spec=ChatCompletionMessage)
        message_1.content = None
        message_1.tool_calls = [tool_call_1]

        # We need model_dump to return a valid dict form for the history append logic
        message_1.model_dump.return_value = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_one", "type": "function", "function": {"name": "create_workspace", "arguments": '{"name": "Quantum Research"}'}}],
        }

        choice_1 = MagicMock(spec=Choice)
        choice_1.message = message_1

        completion_1 = MagicMock(spec=ChatCompletion)
        completion_1.choices = [choice_1]

        # --- Turn 2: LLM wants to save data into that workspace ---
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

        # --- Turn 3: LLM provides its final conclusion text ---
        message_3 = MagicMock(spec=ChatCompletionMessage)
        message_3.content = "Research completely structured and finalized."
        message_3.tool_calls = None
        message_3.model_dump.return_value = {"role": "assistant", "content": "Research completely structured and finalized.", "tool_calls": None}

        choice_3 = MagicMock(spec=Choice)
        choice_3.message = message_3

        completion_3 = MagicMock(spec=ChatCompletion)
        completion_3.choices = [choice_3]

        # Queue up the turns in sequence for the client mock
        mock_openai_client.chat.completions.create.side_effect = [completion_1, completion_2, completion_3]

        # Initialize executor
        executor = AgentExecutor(openai_client=mock_openai_client, orchestrator=mock_orchestrator, system_prompt="Test Prompt")

        # Run the agent execution loop
        result = executor.run("Analyze superconductors.")

        # --- Assertions ---
        # 1. Verify the client was called exactly 3 times (3 loop cycles)
        assert mock_openai_client.chat.completions.create.call_count == 3

        # 2. Verify create_workspace was triggered with args matching Turn 1
        mock_orchestrator.create_workspace.assert_called_once_with(name="Quantum Research")

        # 3. Verify save_memory was triggered with args matching Turn 2
        mock_orchestrator.save_memory.assert_called_once_with(workspace_node_id="workspace_123", content_text="LK-99 retested.", name="Superconductor Fact")

        # 4. Verify the final result matches the closing turn's text payload
        assert result == "Research completely structured and finalized."
