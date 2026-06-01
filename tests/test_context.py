import unittest
from typing import List, Optional, Any
from storage.interface import BaseMemoryStorage
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus
from routing.context import ContextProvider


class MockSearchableStorage(BaseMemoryStorage):
    def __init__(self):
        self.db = {}

    def save(self, memory: MemoryItem) -> None:
        self.db[memory.id] = memory

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        return self.db.get(memory_id)

    def search(self, query: str, scope: MemoryScope, limit: int = 5) -> List[MemoryItem]:
        results = []
        for item in self.db.values():
            if item.scope == scope and query.lower() in str(item.content).lower():
                results.append(item)
        return results[:limit]

    def update(self, memory_id: str, new_content: Any) -> None:
        if memory_id in self.db:
            self.db[memory_id].content = new_content

    def delete(self, memory_id: str) -> None:
        self.db.pop(memory_id, None)


class TestContextProvider(unittest.TestCase):
    def setUp(self):
        self.storage = MockSearchableStorage()
        self.provider = ContextProvider(self.storage)
        self.prep_water = SubTaskState(sub_task_id="sub-prep", title="Boil water", assigned_agent="PrepChef", status=TaskStatus.COMPLETED, outputs={"water_status": "Boiling at 212F"})
        self.cook_pasta = SubTaskState(
            sub_task_id="sub-cook", title="Cook pasta", assigned_agent="LineCook", status=TaskStatus.RUNNING, dependencies=["sub-prep"], inputs={"pasta_type": "spaghetti"}
        )
        self.dinner_task = TaskState(task_id="task-dinner", global_goal="Cook a spaghetti dinner for 4 people", context_data={"table": 5}, sub_tasks=[self.prep_water, self.cook_pasta])

    def test_execution_context_assembly(self):
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        exec_ctx = context_payload["execution_context"]
        self.assertEqual(exec_ctx["assigned_agent"], "LineCook")
        self.assertEqual(exec_ctx["current_sub_task"], "Cook pasta")
        self.assertEqual(exec_ctx["global_context_data"]["table"], 5)
        self.assertEqual(exec_ctx["task_inputs"]["pasta_type"], "spaghetti")

    def test_dependency_outputs_injection(self):
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        deps = context_payload["dependency_outputs"]
        self.assertIn("Boil water", deps)
        self.assertEqual(deps["Boil water"]["water_status"], "Boiling at 212F")

    def test_retrieved_knowledge_with_query(self):
        allergy_memory = MemoryItem(scope=MemoryScope.SESSION, content="Guest at table 5 has a severe gluten allergy. Use gluten-free pasta.")
        self.storage.save(allergy_memory)
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook", query="allergy")
        retrieved = context_payload["retrieved_knowledge"]
        self.assertEqual(len(retrieved), 1)
        self.assertIn("gluten allergy", retrieved[0]["content"])
        self.assertEqual(retrieved[0]["source_scope"], MemoryScope.SESSION.value)

    def test_empty_query_returns_no_knowledge(self):
        self.storage.save(MemoryItem(scope=MemoryScope.SESSION, content="Random fact"))
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        self.assertEqual(len(context_payload["retrieved_knowledge"]), 0)

    def test_invalid_sub_task_id_raises_error(self):
        with self.assertRaises(ValueError) as context:
            self.provider.build_context(self.dinner_task, sub_task_id="ghost-task-123")
        self.assertIn("not found in task", str(context.exception))

    def test_environment_data_injection(self):
        context_payload = self.provider.build_context(self.dinner_task, sub_task_id="sub-cook")
        env_data = context_payload["environment_data"]
        self.assertIn("current_time_utc", env_data)
        epoch_time = env_data["current_time_utc"]
        self.assertIsInstance(epoch_time, float)
        self.assertGreater(epoch_time, 1700000000)


if __name__ == '__main__':
    unittest.main()
