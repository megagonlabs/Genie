from typing import Optional, List, Dict, Any
from storage.interface import BaseMemoryStorage
from models.memory import MemoryItem, MemoryScope
from models.task import TaskState, SubTaskState, TaskStatus


class TaskStateError(Exception):
    pass


class TaskNotFoundError(TaskStateError):
    pass


class TaskFlowManager:
    def __init__(self, storage: BaseMemoryStorage):
        self.storage = storage

    def _get_state_memory_item(self, task_id: str) -> Optional[MemoryItem]:
        return self.storage.get(memory_id=task_id)

    def save_state(self, state: TaskState) -> None:
        existing_item = self._get_state_memory_item(state.task_id)
        state_dict = state.model_dump(mode="json")
        if existing_item:
            self.storage.update(memory_id=state.task_id, new_content=state_dict)
        else:
            state_memory = MemoryItem(id=state.task_id, scope=MemoryScope.SESSION, content=state_dict, metadata={"type": "task_operational_state"})
            self.storage.save(state_memory)

    def load_state(self, task_id: str) -> Optional[TaskState]:
        memory_item = self._get_state_memory_item(task_id)
        if not memory_item:
            return None
        try:
            return TaskState.model_validate(memory_item.content)
        except Exception as e:
            raise ValueError(f"Corrupt state data encountered for task {task_id}") from e

    def add_sub_task(self, task_id: str, sub_task: SubTaskState) -> TaskState:
        state = self.load_state(task_id)
        if not state:
            raise TaskNotFoundError(f"Cannot add sub-task. Parent Task '{task_id}' not found.")
        state.sub_tasks.append(sub_task)
        self.save_state(state)
        return state

    def update_sub_task_status(self, task_id: str, sub_task_id: str, status: TaskStatus, outputs: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> TaskState:
        state = self.load_state(task_id)
        if not state:
            raise TaskNotFoundError(f"Cannot update sub-task. Global Task '{task_id}' not found.")
        for st in state.sub_tasks:
            if st.sub_task_id == sub_task_id:
                st.status = status
                st.outputs = outputs if outputs else {}
                st.error = error
                break
        if status == TaskStatus.FAILED:
            state.status = TaskStatus.FAILED
        else:
            all_done = all(st.status == TaskStatus.COMPLETED for st in state.sub_tasks)
            if all_done and len(state.sub_tasks) > 0:
                state.status = TaskStatus.COMPLETED
        self.save_state(state)
        return state

    def get_runnable_sub_tasks(self, task_id: str) -> List[SubTaskState]:
        state = self.load_state(task_id)
        if not state:
            return []
        completed_ids = {st.sub_task_id for st in state.sub_tasks if st.status == TaskStatus.COMPLETED}
        runnable = []
        for st in state.sub_tasks:
            if st.status == TaskStatus.PENDING:
                if all(dep in completed_ids for dep in st.dependencies):
                    runnable.append(st)
        return runnable

    def clear_state(self, task_id: str) -> None:
        if self._get_state_memory_item(task_id):
            self.storage.delete(memory_id=task_id)
