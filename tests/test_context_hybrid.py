"""Integration Tests for True Hybrid Search and Context Provisioning.

This test suite verifies the end-to-end integration between the ContextProvider
and the actual LocalJSONStorage engine. It writes real files to disk, calculates
real vector embeddings (if FastEmbed is installed), and proves that the Hybrid
Search math successfully ranks semantic meaning over pure keyword stuffing.
"""

import unittest
import os
from storages.local_json import LocalJSONStorage, HAS_FASTEMBED
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus
from routes.context import ContextProvider


class TestContextProviderWithTrueHybridSearch(unittest.TestCase):
    """Integration tests for the real storage engine and context builder."""

    def setUp(self):
        """Sets up a clean, real JSON database on disk before every test."""
        self.test_db_path = "test_hybrid_db.json"

        # Ensure we start with a completely fresh file
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

        self.storage = LocalJSONStorage(file_path=self.test_db_path)
        self.provider = ContextProvider(self.storage)

        # Setup a basic task state to pass into the context builder
        self.cook_pasta = SubTaskState(sub_task_id="sub-cook", title="Cook pasta", assigned_agent="LineCook", status=TaskStatus.RUNNING, inputs={"pasta_type": "spaghetti"})
        self.dinner_task = TaskState(task_id="task-dinner", global_goal="Cook a spaghetti dinner for 4 people", sub_tasks=[self.cook_pasta])

    def tearDown(self):
        """Cleans up the physical test database file after every test."""
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_embeddings_are_generated_on_save(self):
        """Verifies that saving a memory automatically generates a vector array."""
        # Skip this test if the developer running it hasn't installed the vector engine
        if not HAS_FASTEMBED:
            self.skipTest("FastEmbed is not installed. Skipping vector generation test.")

        # Arrange
        memory = MemoryItem(scope=MemoryScope.SESSION, content="The water is boiling.")

        # Act
        self.storage.save(memory)
        saved_memory = self.storage.get(memory.id)

        # Assert: Check that the embedding footprint exists and is a dense vector
        self.assertIn("fastembed-default", saved_memory.embeddings)
        self.assertIsInstance(saved_memory.embeddings["fastembed-default"], list)
        self.assertGreater(len(saved_memory.embeddings["fastembed-default"]), 100)

    def test_true_hybrid_fusion_ranking(self):
        """Proves Hybrid Search is superior to pure keyword search.

        This test explicitly verifies that Reciprocal Rank Fusion correctly
        prioritizes the conceptual meaning of a query over keyword stuffing.
        """
        # Arrange: Create three highly specific test documents

        # 1. High Keyword match, zero conceptual match (Keyword Stuffing)
        doc_lexical = MemoryItem(scope=MemoryScope.SESSION, session_id=self.dinner_task.task_id, content="I bought an apple from the apple store to eat while looking at my apple.")

        # 2. Zero Keyword match, perfect conceptual match (Semantic)
        doc_semantic = MemoryItem(scope=MemoryScope.SESSION, session_id=self.dinner_task.task_id, content="The tech company based in Cupertino makes great smartphones.")

        # 3. High Keyword match, high conceptual match (The Hybrid Winner)
        doc_hybrid = MemoryItem(scope=MemoryScope.SESSION, session_id=self.dinner_task.task_id, content="Apple releases a new iPhone every year.")

        self.storage.save(doc_lexical)
        self.storage.save(doc_semantic)
        self.storage.save(doc_hybrid)

        # Act: Run the search through the ContextProvider
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="Apple smartphone company")
        retrieved = context_payload["retrieved_knowledge"]

        # Assert
        self.assertTrue(len(retrieved) > 0)

        if HAS_FASTEMBED:
            # If vectors are active, the keyword-stuffed fruit document should be penalized
            top_content = retrieved[0]["content"]
            self.assertNotIn("apple store", top_content)

            # The fruit document should be pushed to the very bottom of the results
            contents = [r["content"] for r in retrieved]
            self.assertIn("I bought an apple", contents[-1])
        else:
            # If vectors are OFF (BM25 only), the keyword-stuffed fruit document will win
            self.assertIn("apple store", retrieved[0]["content"])

    def test_context_respects_scoping(self):
        """Verifies that RAG retrieval securely isolates data by scope."""
        # Arrange: Save two memories across different security scopes
        self.storage.save(MemoryItem(scope=MemoryScope.GLOBAL, content="Standard recipe for pasta sauce includes onions."))
        self.storage.save(MemoryItem(scope=MemoryScope.USER, content="This specific user hates onions in pasta."))

        # Act: Search using a global task that does NOT have a user_id specified
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="pasta onions")
        retrieved = context_payload["retrieved_knowledge"]

        # Assert: The system should only confidently return the global fact
        self.assertEqual(len(retrieved), 1)
        self.assertIn("Standard recipe", retrieved[0]["content"])
        self.assertEqual(retrieved[0]["source_scope"], "global")

    def test_environment_epoch_timestamp(self):
        """Verifies the standard formatting of temporal grounding data."""
        # Act
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        epoch_time = context_payload["environment_data"]["current_time_utc"]

        # Assert: Ensure it matches the float timestamp format used by MemoryItems
        self.assertIsInstance(epoch_time, float)
        self.assertGreater(epoch_time, 1700000000.0)


if __name__ == '__main__':
    unittest.main()
