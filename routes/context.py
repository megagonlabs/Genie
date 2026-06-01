"""Context Assembly and Provisioning.

This module is responsible for building the operational context payload that
is fed to the LLM. It combines hard state-awareness (what the agent is doing
right now and what previous agents have completed) with soft retrieved knowledge
(hybrid memory search).
"""

from typing import Dict, Any, Optional
from datetime import datetime, timezone
from storages.interface import BaseMemoryStorage
from models.memory import MemoryScope
from models.task import TaskState, SubTaskState


class ContextProvider:
    """Assembles structured, state-aware context payloads for agent execution.

    This class acts as the bridge between the orchestration engine (DAG) and
    the storage layer. It guarantees that an agent only sees the exact data,
    dependencies, and memories it needs to complete its specific sub-task.
    """

    def __init__(self, storage: BaseMemoryStorage):
        """Initializes the context provider.

        Parameters:
            storage: An implementation of BaseMemoryStorage used to query
                the vector/lexical database for RAG capabilities.
        """
        self.storage = storage

    def _get_sub_task(self, task_state: TaskState, sub_task_id: str) -> Optional[SubTaskState]:
        """Helper to isolate a specific sub-task from the global execution graph.

        Parameters:
            task_state: The overarching global task graph.
            sub_task_id: The unique ID of the sub-task to locate.

        Returns:
            The SubTaskState object if found, otherwise None.
        """
        for st in task_state.sub_tasks:
            if st.sub_task_id == sub_task_id:
                return st
        return None

    def build_context(self, task_state: TaskState, sub_task_id: str, query: str = "", limit: int = 5) -> Dict[str, Any]:
        """Builds a comprehensive, heavily isolated context dictionary for the LLM.

        This method injects state-awareness by looking up exactly what the current
        sub-task is, pulling the outputs of its specific dependencies, and performing
        a scoped hybrid memory search securely filtered by session and user IDs.

        Parameters:
            task_state: The global execution graph for the current workflow.
            sub_task_id: The specific sub-task node the agent is about to execute.
            query: The search string to retrieve relevant semantic knowledge.
            limit: The maximum number of primary memory items to retrieve.

        Returns:
            A structured dictionary ready to be dumped to JSON and injected
            into an LLM prompt.

        Raises:
            ValueError: If the requested sub_task_id does not exist in the TaskState.
        """
        current_sub_task = self._get_sub_task(task_state, sub_task_id)
        if not current_sub_task:
            raise ValueError(f"Sub-task '{sub_task_id}' not found in task '{task_state.task_id}'.")

        # --- Step 1: State Awareness (Execution Context) ---
        # Provide the agent with its immediate directive and global guardrails.
        execution_context = {
            "global_goal": task_state.global_goal,
            "current_sub_task": current_sub_task.title,
            "assigned_agent": current_sub_task.assigned_agent,
            "global_context_data": task_state.context_data,
            "task_inputs": current_sub_task.inputs,
        }

        # --- Step 2: DAG Resolution (Dependency Outputs) ---
        # Pass the exact outputs of preceding tasks to this agent.
        # Keyed by the parent task's title for better LLM semantic understanding.
        dependency_outputs = {}
        for dep_id in current_sub_task.dependencies:
            dep_task = self._get_sub_task(task_state, dep_id)
            if dep_task and dep_task.outputs:
                dependency_outputs[dep_task.title] = dep_task.outputs

        # --- Step 3: Ephemeral & Persistent RAG (Knowledge Retrieval) ---
        retrieved_knowledge = []
        if query:
            # A. Session Scope: Retrieve ephemeral data collected during THIS run.
            # Securely isolated using the task_id as the session boundary.
            session_memories = self.storage.search(query=query, scope=MemoryScope.SESSION, limit=limit, session_id=task_state.task_id)

            # B. Global Scope: Retrieve overarching system guidelines/facts.
            # Usually independent of specific sessions or users.
            global_memories = self.storage.search(query=query, scope=MemoryScope.GLOBAL, limit=limit // 2)

            # C. User Scope: Retrieve personal preferences and constraints.
            # Securely isolated using the user_id extracted from global state.
            current_user_id = task_state.context_data.get("user_id")
            user_memories = self.storage.search(query=query, scope=MemoryScope.USER, limit=limit // 2, user_id=current_user_id)  # Secures the search!

            # Standardize and flatten the retrieved memories for the LLM payload.
            # Note: mem.created_at is safely passed as a float timestamp.
            for mem in session_memories + global_memories + user_memories:
                retrieved_knowledge.append({"content": mem.content, "source_scope": mem.scope.value, "timestamp": mem.created_at})

        # --- Step 4: System Grounding (Environment Data) ---
        # Give the LLM temporal awareness for time-sensitive reasoning.
        environment_data = {"current_time_utc": datetime.now(timezone.utc).timestamp()}

        # --- Step 5: Final Payload Assembly ---
        return {"execution_context": execution_context, "dependency_outputs": dependency_outputs, "retrieved_knowledge": retrieved_knowledge, "environment_data": environment_data}
