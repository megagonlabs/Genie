import unittest
from typing import List, Optional, Any
from storage.interface import BaseMemoryStorage
from routing.state import TaskFlowManager, TaskNotFoundError
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus


class InMemoryStorage(BaseMemoryStorage):

    def __init__(self):
        self.db = {}

    def save(self, memory: MemoryItem) -> None:
        self.db[memory.id] = memory

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        return self.db.get(memory_id)

    def search(self, query: str, scope: MemoryScope, limit: int = 5) -> List[MemoryItem]:
        return [m for m in self.db.values() if m.scope == scope][:limit]

    def update(self, memory_id: str, new_content: Any) -> None:
        if memory_id in self.db:
            self.db[memory_id].content = new_content

    def delete(self, memory_id: str) -> None:
        self.db.pop(memory_id, None)


class TestTaskFlowManager(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryStorage()
        self.manager = TaskFlowManager(self.storage)
        self.dinner_task_id = "task-dinner-001"

    def test_initialize_kitchen_state(self):
        initial_state = TaskState(task_id=self.dinner_task_id, global_goal="Cook a spaghetti dinner for 4 people", status=TaskStatus.RUNNING)
        self.manager.save_state(initial_state)
        loaded_state = self.manager.load_state(self.dinner_task_id)
        self.assertIsNotNone(loaded_state)
        self.assertEqual(loaded_state.global_goal, "Cook a spaghetti dinner for 4 people")

    def test_kitchen_prep_dependencies(self):
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner"))
        prep_water = SubTaskState(
            sub_task_id="sub-prep-water",
            title="Boil water",
            assigned_agent="PrepChef",
        )
        cook_pasta = SubTaskState(sub_task_id="sub-cook-pasta", title="Cook pasta", assigned_agent="LineCook", dependencies=["sub-prep-water"])  # Blocked until water is boiling
        self.manager.add_sub_task(self.dinner_task_id, prep_water)
        self.manager.add_sub_task(self.dinner_task_id, cook_pasta)
        runnable_initial = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertEqual(len(runnable_initial), 1)
        self.assertEqual(runnable_initial[0].sub_task_id, "sub-prep-water")
        self.manager.update_sub_task_status(task_id=self.dinner_task_id, sub_task_id="sub-prep-water", status=TaskStatus.COMPLETED)
        runnable_after = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertEqual(len(runnable_after), 1)
        self.assertEqual(runnable_after[0].sub_task_id, "sub-cook-pasta")

    def test_complex_service_bottleneck(self):
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner"))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="pasta", title="Pasta", assigned_agent="LineCook"))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="sauce", title="Sauce", assigned_agent="SauceChef"))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="serve", title="Serve Dinner", assigned_agent="WaitStaff", dependencies=["pasta", "sauce"]))  # Two dependencies!
        self.manager.update_sub_task_status(self.dinner_task_id, "pasta", TaskStatus.COMPLETED)
        runnable_midway = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertNotIn("serve", [t.sub_task_id for t in runnable_midway])
        self.manager.update_sub_task_status(self.dinner_task_id, "sauce", TaskStatus.COMPLETED)
        runnable_final = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertEqual(runnable_final[0].sub_task_id, "serve")

    def test_global_dinner_completion(self):
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner", status=TaskStatus.RUNNING))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="serve", title="Serve", assigned_agent="WaitStaff"))
        state_after = self.manager.update_sub_task_status(self.dinner_task_id, "serve", TaskStatus.COMPLETED)
        self.assertEqual(state_after.status, TaskStatus.COMPLETED)

    def test_missing_task_exceptions(self):
        with self.assertRaises(TaskNotFoundError):
            self.manager.add_sub_task("wrong-id", SubTaskState(title="Ghost Task", assigned_agent="Nobody"))


if __name__ == '__main__':
    unittest.main()
