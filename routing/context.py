from typing import Dict, Any, Optional
from datetime import datetime, timezone
from storage.interface import BaseMemoryStorage
from models.memory import MemoryScope
from models.task import TaskState, SubTaskState


class ContextProvider:
    def __init__(self, storage: BaseMemoryStorage):
        self.storage = storage

    def _get_sub_task(self, task_state: TaskState, sub_task_id: str) -> Optional[SubTaskState]:
        for st in task_state.sub_tasks:
            if st.sub_task_id == sub_task_id:
                return st
        return None

    def build_context(self, task_state: TaskState, sub_task_id: str, query: str = "", limit: int = 5) -> Dict[str, Any]:
        current_sub_task = self._get_sub_task(task_state, sub_task_id)
        if not current_sub_task:
            raise ValueError(f"Sub-task '{sub_task_id}' not found in task '{task_state.task_id}'.")
        execution_context = {
            "global_goal": task_state.global_goal,
            "current_sub_task": current_sub_task.title,
            "assigned_agent": current_sub_task.assigned_agent,
            "global_context_data": task_state.context_data,
            "task_inputs": current_sub_task.inputs,
        }
        dependency_outputs = {}
        for dep_id in current_sub_task.dependencies:
            dep_task = self._get_sub_task(task_state, dep_id)
            if dep_task and dep_task.outputs:
                dependency_outputs[dep_task.title] = dep_task.outputs
        retrieved_knowledge = []
        if query:
            session_memories = self.storage.search(query=query, scope=MemoryScope.SESSION, limit=limit, session_id=task_state.task_id)
            global_memories = self.storage.search(query=query, scope=MemoryScope.GLOBAL, limit=limit // 2)
            current_user_id = task_state.context_data.get("user_id")
            user_memories = self.storage.search(query=query, scope=MemoryScope.USER, limit=limit // 2, user_id=current_user_id)  # Secures the search!
            for mem in session_memories + global_memories + user_memories:
                retrieved_knowledge.append({"content": mem.content, "source_scope": mem.scope.value, "timestamp": mem.created_at})
        environment_data = {"current_time_utc": datetime.now(timezone.utc).timestamp()}
        return {"execution_context": execution_context, "dependency_outputs": dependency_outputs, "retrieved_knowledge": retrieved_knowledge, "environment_data": environment_data}
