import unittest
import os
from storage.local_json import LocalJSONStorage, HAS_FASTEMBED
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus
from routing.context import ContextProvider


class TestContextProviderWithTrueHybridSearch(unittest.TestCase):
    def setUp(self):
        self.test_db_path = "test_hybrid_db.json"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.storage = LocalJSONStorage(file_path=self.test_db_path)
        self.provider = ContextProvider(self.storage)
        self.cook_pasta = SubTaskState(sub_task_id="sub-cook", title="Cook pasta", assigned_agent="LineCook", status=TaskStatus.RUNNING, inputs={"pasta_type": "spaghetti"})
        self.dinner_task = TaskState(task_id="task-dinner", global_goal="Cook a spaghetti dinner for 4 people", sub_tasks=[self.cook_pasta])

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_embeddings_are_generated_on_save(self):
        if not HAS_FASTEMBED:
            self.skipTest("FastEmbed is not installed. Skipping vector generation test.")
        memory = MemoryItem(scope=MemoryScope.SESSION, content="The water is boiling.")
        self.storage.save(memory)
        saved_memory = self.storage.get(memory.id)
        self.assertIn("fastembed-default", saved_memory.embeddings)
        self.assertIsInstance(saved_memory.embeddings["fastembed-default"], list)
        self.assertGreater(len(saved_memory.embeddings["fastembed-default"]), 100)

    def test_true_hybrid_fusion_ranking(self):
        doc_lexical = MemoryItem(scope=MemoryScope.SESSION, content="I bought an apple from the apple store to eat while looking at my apple.")
        doc_semantic = MemoryItem(scope=MemoryScope.SESSION, content="The tech company based in Cupertino makes great smartphones.")
        doc_hybrid = MemoryItem(scope=MemoryScope.SESSION, content="Apple releases a new iPhone every year.")
        self.storage.save(doc_lexical)
        self.storage.save(doc_semantic)
        self.storage.save(doc_hybrid)
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="Apple smartphone company")
        retrieved = context_payload["retrieved_knowledge"]
        self.assertTrue(len(retrieved) > 0)
        if HAS_FASTEMBED:
            top_content = retrieved[0]["content"]
            self.assertNotIn("apple store", top_content)
            contents = [r["content"] for r in retrieved]
            self.assertIn("I bought an apple", contents[-1])
        else:
            self.assertIn("apple store", retrieved[0]["content"])

    def test_context_respects_scoping(self):
        self.storage.save(MemoryItem(scope=MemoryScope.GLOBAL, content="Standard recipe for pasta sauce includes onions."))
        self.storage.save(MemoryItem(scope=MemoryScope.USER, content="This specific user hates onions in pasta."))
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="pasta onions")
        retrieved = context_payload["retrieved_knowledge"]
        self.assertEqual(len(retrieved), 1)
        self.assertIn("Standard recipe", retrieved[0]["content"])
        self.assertEqual(retrieved[0]["source_scope"], "global")

    def test_environment_epoch_timestamp(self):
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        epoch_time = context_payload["environment_data"]["current_time_utc"]
        self.assertIsInstance(epoch_time, float)
        self.assertGreater(epoch_time, 1700000000.0)


if __name__ == '__main__':
    unittest.main()
