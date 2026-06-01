"""Unit Tests for Context Assembly and Provisioning.

This test suite verifies that the ContextProvider accurately constructs the
JSON payload for the LLM. It ensures that the global state, dependency outputs,
environment variables, and RAG knowledge are correctly isolated and injected.
"""

import unittest
from typing import List, Optional, Any
from storages.interface import BaseMemoryStorage
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus
from routings.context import ContextProvider


class MockSearchableStorage(BaseMemoryStorage):
    """A lightweight, purely in-memory storage double with basic search capabilities.

    This mocks the LocalJSONStorage engine, allowing us to test semantic RAG
    retrieval logic without generating heavy vector embeddings or writing to disk.
    """

    def __init__(self):
        self.db = {}

    def save(self, memory: MemoryItem) -> None:
        self.db[memory.id] = memory

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        return self.db.get(memory_id)

    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:
        """A simple string-matching mock of the hybrid search engine."""
        results = []
        for item in self.db.values():
            # Basic keyword match and scope check
            if item.scope == scope and query.lower() in str(item.content).lower():

                # Mock the strict isolation boundaries
                if session_id and item.session_id != session_id:
                    continue
                if user_id and item.user_id != user_id:
                    continue

                results.append(item)

        return results[:limit]

    def update(self, memory_id: str, new_content: Any) -> None:
        if memory_id in self.db:
            self.db[memory_id].content = new_content

    def delete(self, memory_id: str) -> None:
        self.db.pop(memory_id, None)


class TestContextProvider(unittest.TestCase):
    """Test cases for the prompt context payload builder."""

    def setUp(self):
        """Sets up the kitchen scenario before every test."""
        self.storage = MockSearchableStorage()
        self.provider = ContextProvider(self.storage)

        # Create a completed dependency task
        self.prep_water = SubTaskState(sub_task_id="sub-prep", title="Boil water", assigned_agent="PrepChef", status=TaskStatus.COMPLETED, outputs={"water_status": "Boiling at 212F"})

        # Create the active task that depends on the water
        self.cook_pasta = SubTaskState(
            sub_task_id="sub-cook", title="Cook pasta", assigned_agent="LineCook", status=TaskStatus.RUNNING, dependencies=["sub-prep"], inputs={"pasta_type": "spaghetti"}
        )

        # Wrap them in the global TaskState
        self.dinner_task = TaskState(task_id="task-dinner", global_goal="Cook a spaghetti dinner for 4 people", context_data={"table": 5}, sub_tasks=[self.prep_water, self.cook_pasta])

    def test_execution_context_assembly(self):
        """Verifies that the immediate execution directives are correctly parsed."""
        # Act
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        exec_ctx = context_payload["execution_context"]

        # Assert: Check agent role, current objective, and inputs
        self.assertEqual(exec_ctx["assigned_agent"], "LineCook")
        self.assertEqual(exec_ctx["current_sub_task"], "Cook pasta")
        self.assertEqual(exec_ctx["global_context_data"]["table"], 5)
        self.assertEqual(exec_ctx["task_inputs"]["pasta_type"], "spaghetti")

    def test_dependency_outputs_injection(self):
        """Verifies that upstream task results are successfully passed to downstream agents."""
        # Act
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        deps = context_payload["dependency_outputs"]

        # Assert: The LineCook needs to know the water is already boiling
        self.assertIn("Boil water", deps)
        self.assertEqual(deps["Boil water"]["water_status"], "Boiling at 212F")

    def test_retrieved_knowledge_with_query(self):
        """Verifies that RAG search accurately fetches isolated session memories."""
        # Arrange: Save a memory strictly tied to this specific dinner session
        allergy_memory = MemoryItem(
            scope=MemoryScope.SESSION, session_id=self.dinner_task.task_id, content="Guest at table 5 has a severe gluten allergy. Use gluten-free pasta."  # Secures it to this run!
        )
        self.storage.save(allergy_memory)

        # Act: Build context with a search query
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="allergy")
        retrieved = context_payload["retrieved_knowledge"]

        # Assert: The LLM should receive the critical safety knowledge
        self.assertEqual(len(retrieved), 1)
        self.assertIn("gluten allergy", retrieved[0]["content"])
        self.assertEqual(retrieved[0]["source_scope"], MemoryScope.SESSION.value)

    def test_empty_query_returns_no_knowledge(self):
        """Verifies that passing no query string bypasses the search engine."""
        # Arrange
        self.storage.save(MemoryItem(scope=MemoryScope.SESSION, content="Random fact"))

        # Act: Pass an empty query string
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="")

        # Assert: No knowledge should be retrieved
        self.assertEqual(len(context_payload["retrieved_knowledge"]), 0)

    def test_invalid_sub_task_id_raises_error(self):
        """Verifies that the provider crashes safely if given a broken ID."""
        # Act & Assert
        with self.assertRaises(ValueError) as context:
            self.provider.build_context(self.dinner_task, sub_task_id="ghost-task-123")

        self.assertIn("not found in task", str(context.exception))

    def test_environment_data_injection(self):
        """Verifies that the temporal grounding data (timestamps) are correctly formatted floats."""
        # Act
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        env_data = context_payload["environment_data"]

        # Assert
        self.assertIn("current_time_utc", env_data)
        epoch_time = env_data["current_time_utc"]
        self.assertIsInstance(epoch_time, float)
        self.assertGreater(epoch_time, 1700000000)


if __name__ == '__main__':
    unittest.main()
