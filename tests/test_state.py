"""Unit Tests for Task Flow and DAG Resolution.

This test suite verifies that the TaskFlowManager correctly handles state
transitions, resolves complex task dependencies (the DAG), and properly
persists execution data to the storage layer.
"""

import unittest
from typing import List, Optional, Any
from storages.interface import BaseMemoryStorage
from routes.state import TaskFlowManager, TaskNotFoundError
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus


class InMemoryStorage(BaseMemoryStorage):
    """A lightweight, purely in-memory storage double for unit testing.

    We use this instead of LocalJSONStorage so our tests run instantly
    and don't leave residual 'db.json' files on the developer's machine.
    """

    def __init__(self):
        self.db = {}

    def save(self, memory: MemoryItem) -> None:
        self.db[memory.id] = memory

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        return self.db.get(memory_id)

    def search(self, query: str, scope: MemoryScope, limit: int = 5, session_id: Optional[str] = None, user_id: Optional[str] = None) -> List[MemoryItem]:
        # Simplified mock search: just filters by scope and limit
        return [m for m in self.db.values() if m.scope == scope][:limit]

    def update(self, memory_id: str, new_content: Any) -> None:
        if memory_id in self.db:
            self.db[memory_id].content = new_content

    def delete(self, memory_id: str) -> None:
        self.db.pop(memory_id, None)


class TestTaskFlowManager(unittest.TestCase):
    """Test cases for the core workflow orchestration logic."""

    def setUp(self):
        """Runs before every single test to provide a clean slate."""
        self.storage = InMemoryStorage()
        self.manager = TaskFlowManager(self.storage)
        self.dinner_task_id = "task-dinner-001"

    def test_initialize_kitchen_state(self):
        """Verifies that a global task state can be correctly saved and loaded."""
        # Arrange: Create the initial state
        initial_state = TaskState(task_id=self.dinner_task_id, global_goal="Cook a spaghetti dinner for 4 people", status=TaskStatus.RUNNING)

        # Act: Save and immediately reload it
        self.manager.save_state(initial_state)
        loaded_state = self.manager.load_state(self.dinner_task_id)

        # Assert: Ensure data integrity was maintained
        self.assertIsNotNone(loaded_state)
        self.assertEqual(loaded_state.global_goal, "Cook a spaghetti dinner for 4 people")

    def test_kitchen_prep_dependencies(self):
        """Verifies linear dependency resolution (Task A must finish before Task B)."""
        # Arrange: Setup global task and two sequential sub-tasks
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner"))

        prep_water = SubTaskState(
            sub_task_id="sub-prep-water",
            title="Boil water",
            assigned_agent="PrepChef",
        )
        cook_pasta = SubTaskState(sub_task_id="sub-cook-pasta", title="Cook pasta", assigned_agent="LineCook", dependencies=["sub-prep-water"])  # Blocked until water is boiling

        self.manager.add_sub_task(self.dinner_task_id, prep_water)
        self.manager.add_sub_task(self.dinner_task_id, cook_pasta)

        # Act & Assert 1: Only 'prep_water' should be runnable right now
        runnable_initial = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertEqual(len(runnable_initial), 1)
        self.assertEqual(runnable_initial[0].sub_task_id, "sub-prep-water")

        # Act 2: Simulate the PrepChef finishing their job
        self.manager.update_sub_task_status(task_id=self.dinner_task_id, sub_task_id="sub-prep-water", status=TaskStatus.COMPLETED)

        # Assert 2: 'cook_pasta' should now be unblocked and runnable
        runnable_after = self.manager.get_runnable_sub_tasks(self.dinner_task_id)
        self.assertEqual(len(runnable_after), 1)
        self.assertEqual(runnable_after[0].sub_task_id, "sub-cook-pasta")

    def test_complex_service_bottleneck(self):
        """Verifies multi-dependency resolution (Task C requires both Task A and Task B)."""
        # Arrange: Setup the DAG where serving requires both pasta AND sauce
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner"))

        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="pasta", title="Pasta", assigned_agent="LineCook"))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="sauce", title="Sauce", assigned_agent="SauceChef"))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="serve", title="Serve Dinner", assigned_agent="WaitStaff", dependencies=["pasta", "sauce"]))

        # Act 1: Only the pasta is done. Sauce is still cooking.
        self.manager.update_sub_task_status(self.dinner_task_id, "pasta", TaskStatus.COMPLETED)
        runnable_midway = self.manager.get_runnable_sub_tasks(self.dinner_task_id)

        # Assert 1: The WaitStaff should NOT be allowed to serve yet
        self.assertNotIn("serve", [t.sub_task_id for t in runnable_midway])

        # Act 2: Sauce is now done.
        self.manager.update_sub_task_status(self.dinner_task_id, "sauce", TaskStatus.COMPLETED)
        runnable_final = self.manager.get_runnable_sub_tasks(self.dinner_task_id)

        # Assert 2: WaitStaff is unblocked
        self.assertEqual(runnable_final[0].sub_task_id, "serve")

    def test_global_dinner_completion(self):
        """Verifies the global task status auto-updates when all sub-tasks finish."""
        # Arrange
        self.manager.save_state(TaskState(task_id=self.dinner_task_id, global_goal="Dinner", status=TaskStatus.RUNNING))
        self.manager.add_sub_task(self.dinner_task_id, SubTaskState(sub_task_id="serve", title="Serve", assigned_agent="WaitStaff"))

        # Act: Complete the final sub-task
        state_after = self.manager.update_sub_task_status(self.dinner_task_id, "serve", TaskStatus.COMPLETED)

        # Assert: The overarching task manager should realize the whole graph is done
        self.assertEqual(state_after.status, TaskStatus.COMPLETED)

    def test_missing_task_exceptions(self):
        """Verifies that the system safely rejects operations on non-existent global tasks."""
        with self.assertRaises(TaskNotFoundError):
            self.manager.add_sub_task("wrong-id", SubTaskState(title="Ghost Task", assigned_agent="Nobody"))


if __name__ == '__main__':
    unittest.main()
