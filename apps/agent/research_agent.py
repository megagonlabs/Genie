"""Reusable deep-research agent workflow built on top of Genie memory tools."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from genie.core.orchestrator import MemoryOrchestrator
from genie.integrations.agent_tools import build_memory_tool_registry, get_memory_tool_schemas
from genie.models.node import Node

from apps.agent.executor import ToolLoopExecutor
from apps.agent.models import (
    ProgressEvent,
    ResearchRunResult,
    ResearchRunVerification,
    ToolLoopResult,
    VerificationCheck,
)
from apps.agent.prompts import (
    EXECUTION_PROMPT,
    PLANNING_PROMPT,
    SYNTHESIS_PROMPT,
    build_execution_user_prompt,
    build_planning_user_prompt,
    build_synthesis_user_prompt,
)

ProgressCallback = Callable[[ProgressEvent], None]


class DeepResearchAgent:
    """Plan, execute, and synthesize long-running research with Genie memory.

    Parameters:
    openai_client:
        OpenAI client used for planning, execution, and synthesis.
    orchestrator:
        Genie orchestrator that exposes workspace, memory-save, and search tools.
    planning_model:
        Model used for the structured planning pass.
    execution_model:
        Model used for the long-running tool loop.
    synthesis_model:
        Model used to write the final report from recalled memories.
    max_tool_round_trips:
        Safety limit for the execution loop.
    """

    def __init__(
        self,
        openai_client: OpenAI,
        orchestrator: MemoryOrchestrator,
        planning_model: str = "gpt-4o",
        execution_model: str = "gpt-4o",
        synthesis_model: str = "gpt-4o",
        max_tool_round_trips: int = 30,
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.client = openai_client
        self.orchestrator = orchestrator
        self.planning_model = planning_model
        self.synthesis_model = synthesis_model
        self.on_progress = on_progress
        self._active_run_id: Optional[str] = None
        self.tool_executor = ToolLoopExecutor(
            openai_client=openai_client,
            model=execution_model,
            tools=get_memory_tool_schemas(),
            available_functions=build_memory_tool_registry(orchestrator),
            max_round_trips=max_tool_round_trips,
            on_progress=self._handle_executor_progress,
        )

    def run(self, user_request: str) -> str:
        """Execute the full deep-research workflow.

        Parameters:
        user_request:
            The large research task to investigate and synthesize.

        Returns:
        str
            Final comprehensive report.
        """
        return self.run_with_audit(user_request).final_report

    def run_with_audit(self, user_request: str) -> ResearchRunResult:
        """Execute the full workflow and return a verification-rich result bundle.

        Parameters:
        user_request:
            The large research task to investigate and synthesize.

        Returns:
        ResearchRunResult
            Final report together with execution trace and verification details.
        """
        run_id = f"run_{uuid.uuid4().hex}"
        self._active_run_id = run_id
        self._emit_progress(
            stage="run",
            status="started",
            message="Started deep research run.",
            run_id=run_id,
        )
        try:
            self._emit_progress(
                stage="planning",
                status="started",
                message="Building structured research plan.",
                run_id=run_id,
            )
            plan = self._build_plan(user_request)
            self._emit_progress(
                stage="planning",
                status="completed",
                message="Structured research plan created.",
                run_id=run_id,
                data={"subtask_count": len(plan.get("subtasks", [])) if isinstance(plan.get("subtasks"), list) else 0},
            )
            serialized_plan = json.dumps(plan, indent=2)

            self._emit_progress(
                stage="setup",
                status="started",
                message="Creating audit and research workspaces for this run.",
                run_id=run_id,
            )
            audit_ids = self._initialize_run_tree(run_id=run_id, user_request=user_request, plan=plan)
            self._emit_progress(
                stage="setup",
                status="completed",
                message="Run workspace tree is ready.",
                run_id=run_id,
                data={"research_root_node_id": audit_ids.get("research_root_id")},
            )
            self._persist_artifact(
                artifact_name="plan",
                parent_id=audit_ids.get("plan_workspace_id"),
                title="research_plan",
                content_text=serialized_plan,
                metadata={
                    "run_id": run_id,
                    "stage": "planning",
                    "artifact_type": "research_plan",
                    "created_at": self._timestamp(),
                    "user_request": user_request,
                },
            )

            self._emit_progress(
                stage="execution",
                status="started",
                message="Executing research plan with memory tools.",
                run_id=run_id,
            )
            execution_trace = self._execute_plan(
                user_request=user_request,
                serialized_plan=serialized_plan,
                research_root_node_id=self._normalize_optional_id(audit_ids.get("research_root_id")),
            )
            execution_summary = execution_trace.final_output
            saved_memory_ids = self._collect_returned_node_ids(execution_trace.tool_calls, "save_memory")
            created_workspace_ids = self._collect_returned_node_ids(execution_trace.tool_calls, "create_workspace")
            self._emit_progress(
                stage="execution",
                status="completed",
                message="Execution phase completed.",
                run_id=run_id,
                data={
                    "tool_call_count": len(execution_trace.tool_calls),
                    "saved_memory_count": len(saved_memory_ids),
                    "created_workspace_count": len(created_workspace_ids),
                },
            )
            self._persist_artifact(
                artifact_name="execution",
                parent_id=audit_ids.get("execution_workspace_id"),
                title="execution_summary",
                content_text=execution_summary,
                metadata={
                    "run_id": run_id,
                    "stage": "execution",
                    "artifact_type": "execution_summary",
                    "created_at": self._timestamp(),
                    "assistant_turn_count": execution_trace.assistant_turn_count,
                    "tool_call_count": len(execution_trace.tool_calls),
                },
                relationships={
                    "saved_memory_node_ids": saved_memory_ids,
                    "created_workspace_node_ids": created_workspace_ids,
                },
            )

            self._emit_progress(
                stage="recall",
                status="started",
                message="Recalling stored memories for synthesis.",
                run_id=run_id,
            )
            recalled_memories = self._recall_memories(
                plan,
                scope_node_id=self._normalize_optional_id(audit_ids.get("research_root_id")),
            )
            recall_artifact_ids, recalled_node_ids = self._persist_recall_artifacts(
                run_id=run_id,
                recall_workspace_id=audit_ids.get("recall_workspace_id"),
                recalled_memories=recalled_memories,
            )
            self._emit_progress(
                stage="recall",
                status="completed",
                message="Recall phase completed.",
                run_id=run_id,
                data={
                    "query_count": len(recalled_memories),
                    "recalled_node_count": len(recalled_node_ids),
                },
            )

            self._emit_progress(
                stage="synthesis",
                status="started",
                message="Writing final report from stored work.",
                run_id=run_id,
            )
            final_report = self._synthesize_report(
                user_request=user_request,
                serialized_plan=serialized_plan,
                execution_summary=execution_summary,
                recalled_memories=json.dumps(recalled_memories, indent=2),
            )
            self._emit_progress(
                stage="synthesis",
                status="completed",
                message="Final report generated.",
                run_id=run_id,
                data={"report_preview": final_report[:160]},
            )
            final_report_artifact_id = self._persist_artifact(
                artifact_name="final_report",
                parent_id=audit_ids.get("report_workspace_id"),
                title="final_report",
                content_text=final_report,
                metadata={
                    "run_id": run_id,
                    "stage": "report",
                    "artifact_type": "final_report",
                    "created_at": self._timestamp(),
                },
                relationships={
                    "source_node_ids": recalled_node_ids,
                    "recall_artifact_ids": recall_artifact_ids,
                    "saved_memory_node_ids": saved_memory_ids,
                },
            )
            self._emit_progress(
                stage="verification",
                status="started",
                message="Verifying that the run used multi-step memory-backed work.",
                run_id=run_id,
            )
            verification = self._verify_run(
                run_id=run_id,
                plan=plan,
                execution_trace=execution_trace,
                recalled_memories=recalled_memories,
                audit_root_node_id=audit_ids.get("audit_root_id"),
                research_root_node_id=audit_ids.get("research_root_id"),
                artifact_node_ids={
                    "plan_workspace": audit_ids.get("plan_workspace_id", ""),
                    "execution_workspace": audit_ids.get("execution_workspace_id", ""),
                    "recall_workspace": audit_ids.get("recall_workspace_id", ""),
                    "report_workspace": audit_ids.get("report_workspace_id", ""),
                    "verification_workspace": audit_ids.get("verification_workspace_id", ""),
                    "final_report": final_report_artifact_id or "",
                },
            )
            verification_artifact_id = self._persist_artifact(
                artifact_name="verification",
                parent_id=audit_ids.get("verification_workspace_id"),
                title="verification_summary",
                content_text=json.dumps(verification.model_dump(), indent=2),
                metadata={
                    "run_id": run_id,
                    "stage": "verification",
                    "artifact_type": "verification_summary",
                    "created_at": self._timestamp(),
                    "verification_status": verification.status,
                },
                relationships={
                    "final_report_artifact_ids": [final_report_artifact_id] if final_report_artifact_id else [],
                    "saved_memory_node_ids": saved_memory_ids,
                    "recalled_node_ids": recalled_node_ids,
                },
            )
            if verification_artifact_id:
                verification.artifact_node_ids["verification_summary"] = verification_artifact_id
            self._emit_progress(
                stage="verification",
                status="completed",
                message=f"Verification completed with status {verification.status}.",
                run_id=run_id,
                data={"verification_status": verification.status},
            )

            result = ResearchRunResult(
                run_id=run_id,
                final_report=final_report,
                plan=plan,
                execution_summary=execution_summary,
                recalled_memories=recalled_memories,
                execution_trace=execution_trace,
                verification=verification,
                audit_root_node_id=audit_ids.get("audit_root_id"),
                research_root_node_id=audit_ids.get("research_root_id"),
            )
            self._emit_progress(
                stage="run",
                status="completed",
                message="Deep research run finished.",
                run_id=run_id,
                data={"verification_status": verification.status},
            )
            return result
        except Exception as exc:
            self._emit_progress(
                stage="run",
                status="failed",
                message=f"Deep research run failed: {exc}",
                run_id=run_id,
                data={"error_message": str(exc)},
            )
            raise
        finally:
            self._active_run_id = None

    def _build_plan(self, user_request: str) -> Dict[str, Any]:
        """Generate a structured research plan from the user request."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": PLANNING_PROMPT},
            {"role": "user", "content": build_planning_user_prompt(user_request)},
        ]
        response = self.client.chat.completions.create(
            model=self.planning_model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    def _initialize_run_tree(self, run_id: str, user_request: str, plan: Dict[str, Any]) -> Dict[str, str]:
        """Create a dedicated audit and research subtree for the current run."""
        topic_title = self._plan_topic_title(plan) or self._slugify(user_request) or "research"
        audit_root_id = self._create_workspace_artifact(
            name=f"{topic_title}_{run_id}",
            parent_id=None,
            metadata={
                "run_id": run_id,
                "stage": "run_root",
                "artifact_type": "run_root",
                "created_at": self._timestamp(),
                "user_request": user_request,
            },
        )
        research_root_id = self._create_workspace_artifact(
            name="research_tree",
            parent_id=audit_root_id,
            metadata={
                "run_id": run_id,
                "stage": "research_root",
                "artifact_type": "research_root",
                "created_at": self._timestamp(),
            },
        )
        audit_workspace_id = self._create_workspace_artifact(
            name="audit",
            parent_id=audit_root_id,
            metadata={
                "run_id": run_id,
                "stage": "audit_root",
                "artifact_type": "audit_root",
                "created_at": self._timestamp(),
            },
        )
        plan_workspace_id = self._create_workspace_artifact("plan", audit_workspace_id, {"run_id": run_id, "stage": "plan_workspace"})
        execution_workspace_id = self._create_workspace_artifact(
            "execution",
            audit_workspace_id,
            {"run_id": run_id, "stage": "execution_workspace"},
        )
        recall_workspace_id = self._create_workspace_artifact(
            "recalled_memories",
            audit_workspace_id,
            {"run_id": run_id, "stage": "recall_workspace"},
        )
        report_workspace_id = self._create_workspace_artifact(
            "report",
            audit_workspace_id,
            {"run_id": run_id, "stage": "report_workspace"},
        )
        verification_workspace_id = self._create_workspace_artifact(
            "verification",
            audit_workspace_id,
            {"run_id": run_id, "stage": "verification_workspace"},
        )
        return {
            "audit_root_id": audit_root_id or "",
            "research_root_id": research_root_id or "",
            "audit_workspace_id": audit_workspace_id or "",
            "plan_workspace_id": plan_workspace_id or "",
            "execution_workspace_id": execution_workspace_id or "",
            "recall_workspace_id": recall_workspace_id or "",
            "report_workspace_id": report_workspace_id or "",
            "verification_workspace_id": verification_workspace_id or "",
        }

    def _execute_plan(
        self,
        user_request: str,
        serialized_plan: str,
        research_root_node_id: Optional[str],
    ) -> ToolLoopResult:
        """Run the long-lived tool loop that populates hierarchical memory."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": EXECUTION_PROMPT},
            {
                "role": "user",
                "content": build_execution_user_prompt(
                    user_request,
                    serialized_plan,
                    research_root_node_id=research_root_node_id,
                ),
            },
        ]
        return self.tool_executor.run_detailed(messages)

    def _recall_memories(
        self,
        plan: Dict[str, Any],
        scope_node_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recall final supporting memories using the plan's synthesis queries."""
        queries = plan.get("final_search_queries", [])
        if not isinstance(queries, list):
            return []

        recalled_results: List[Dict[str, Any]] = []
        for query in queries:
            if not isinstance(query, str) or not query.strip():
                continue
            normalized_query = query.strip()
            self._emit_progress(
                stage="recall",
                status="info",
                message=f"Searching memory for '{normalized_query}'.",
                run_id=self._active_run_id,
                data={"query": normalized_query, "scope_node_id": scope_node_id},
            )
            results = self.orchestrator.search_memory(
                query_text=normalized_query,
                scope_node_id=scope_node_id,
                top_k=8,
            )
            recalled_results.append(
                {
                    "query": normalized_query,
                    "results": results,
                }
            )
            self._emit_progress(
                stage="recall",
                status="info",
                message=f"Recall query '{normalized_query}' returned {len(results)} result(s).",
                run_id=self._active_run_id,
                data={"query": normalized_query, "result_count": len(results)},
            )
        return recalled_results

    def _synthesize_report(
        self,
        user_request: str,
        serialized_plan: str,
        execution_summary: str,
        recalled_memories: str,
    ) -> str:
        """Write the final report from the plan, execution summary, and recalled memories."""
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {
                "role": "user",
                "content": build_synthesis_user_prompt(
                    user_request=user_request,
                    serialized_plan=serialized_plan,
                    execution_summary=execution_summary,
                    recalled_memories=recalled_memories,
                ),
            },
        ]
        response = self.client.chat.completions.create(
            model=self.synthesis_model,
            messages=messages,
        )
        return response.choices[0].message.content or "Task completed with no text output."

    def _verify_run(
        self,
        *,
        run_id: str,
        plan: Dict[str, Any],
        execution_trace: ToolLoopResult,
        recalled_memories: List[Dict[str, Any]],
        audit_root_node_id: Optional[str],
        research_root_node_id: Optional[str],
        artifact_node_ids: Dict[str, str],
    ) -> ResearchRunVerification:
        """Evaluate whether the run exhibits healthy multi-step research behavior."""
        subtasks = plan.get("subtasks", [])
        subtask_count = len(subtasks) if isinstance(subtasks, list) else 0
        valid_queries = self._valid_final_queries(plan)
        required_memory_writes = max(1, subtask_count) if subtask_count else 1
        workspace_calls = [trace for trace in execution_trace.tool_calls if trace.tool_name == "create_workspace" and trace.succeeded]
        save_calls = [trace for trace in execution_trace.tool_calls if trace.tool_name == "save_memory" and trace.succeeded]
        search_calls = [trace for trace in execution_trace.tool_calls if trace.tool_name == "search_memory" and trace.succeeded]
        saved_memory_ids = {trace.returned_node_id for trace in save_calls if trace.returned_node_id}
        recalled_node_ids = self._collect_recalled_node_ids(recalled_memories)
        overlap_node_ids = sorted(saved_memory_ids.intersection(recalled_node_ids))
        anchored_workspace_calls = [trace for trace in workspace_calls if research_root_node_id and trace.arguments.get("parent_id") == research_root_node_id]

        checks: List[VerificationCheck] = []
        self._append_check(
            checks,
            name="tool_loop_used_multiple_steps",
            passed=len(execution_trace.tool_calls) >= max(2, subtask_count),
            failure_details=("The run used too few tool calls to demonstrate iterative memory-backed work."),
            success_details=(f"The run used {len(execution_trace.tool_calls)} tool calls across " f"{execution_trace.assistant_turn_count} assistant turns."),
        )
        self._append_check(
            checks,
            name="intermediate_memories_saved",
            passed=len(save_calls) >= required_memory_writes,
            failure_details=("The run did not save enough intermediate memories to rule out one-shot generation."),
            success_details=(f"The run saved {len(save_calls)} intermediate memory nodes before final synthesis."),
        )
        self._append_check(
            checks,
            name="research_tree_anchored",
            passed=bool(research_root_node_id) and bool(anchored_workspace_calls or subtask_count == 0),
            failure_details=("The execution did not clearly anchor created workspaces under the dedicated run subtree."),
            success_details=(f"{len(anchored_workspace_calls)} workspace creations were explicitly anchored under " "the run's research root."),
        )
        if valid_queries:
            self._append_check(
                checks,
                name="final_recall_performed",
                passed=len(recalled_memories) == len(valid_queries),
                failure_details="Not all planned final recall queries were executed before synthesis.",
                success_details=(f"Executed {len(recalled_memories)} planned recall queries before writing the report."),
            )
            self._append_check(
                checks,
                name="recalled_memories_overlap_saved_work",
                passed=bool(overlap_node_ids),
                failure_details=("The final recall results did not point back to memories created during this run."),
                success_details=(f"The final recall phase reused {len(overlap_node_ids)} memories produced earlier in the run."),
            )
        else:
            checks.append(
                VerificationCheck(
                    name="final_recall_performed",
                    status="warn",
                    details="The plan defined no final recall queries, so recall grounding could not be verified.",
                )
            )

        warnings: List[str] = []
        if not search_calls and valid_queries:
            warnings.append("The execution loop itself never called search_memory. Recall happened only in the explicit pre-synthesis pass.")
        if len(save_calls) < max(2, required_memory_writes):
            warnings.append("The run met the minimum memory-write threshold, but saving more intermediate notes would strengthen provenance.")
        if valid_queries and not overlap_node_ids:
            warnings.append("Final recall did not overlap with current-run saved memories, so the report may rely on weak grounding.")

        metrics = {
            "subtask_count": subtask_count,
            "planned_final_query_count": len(valid_queries),
            "assistant_turn_count": execution_trace.assistant_turn_count,
            "tool_call_count": len(execution_trace.tool_calls),
            "workspace_create_count": len(workspace_calls),
            "memory_write_count": len(save_calls),
            "execution_search_call_count": len(search_calls),
            "recalled_query_count": len(recalled_memories),
            "recalled_result_count": sum(len(result_bundle.get("results", [])) for result_bundle in recalled_memories if isinstance(result_bundle.get("results"), list)),
            "saved_memory_node_ids": sorted(saved_memory_ids),
            "recalled_node_ids": sorted(recalled_node_ids),
            "overlap_node_ids": overlap_node_ids,
        }
        statuses = {check.status for check in checks}
        if "fail" in statuses:
            overall_status = "fail"
        elif warnings or "warn" in statuses:
            overall_status = "warn"
        else:
            overall_status = "pass"
        return ResearchRunVerification(
            run_id=run_id,
            status=overall_status,
            checks=checks,
            metrics=metrics,
            warnings=warnings,
            audit_root_node_id=audit_root_node_id,
            research_root_node_id=research_root_node_id,
            artifact_node_ids={key: value for key, value in artifact_node_ids.items() if value},
        )

    def _persist_recall_artifacts(
        self,
        *,
        run_id: str,
        recall_workspace_id: Optional[str],
        recalled_memories: List[Dict[str, Any]],
    ) -> Tuple[List[str], List[str]]:
        """Persist per-query recall snapshots for later inspection."""
        artifact_ids: List[str] = []
        recalled_node_ids = sorted(self._collect_recalled_node_ids(recalled_memories))
        for index, recalled_bundle in enumerate(recalled_memories, start=1):
            query = recalled_bundle.get("query", f"query_{index}")
            query_node_ids = sorted(self._collect_recalled_node_ids([recalled_bundle]))
            artifact_id = self._persist_artifact(
                artifact_name=f"recall_{index}",
                parent_id=recall_workspace_id,
                title=f"recall_query_{index}",
                content_text=json.dumps(recalled_bundle, indent=2),
                metadata={
                    "run_id": run_id,
                    "stage": "recall",
                    "artifact_type": "recall_snapshot",
                    "created_at": self._timestamp(),
                    "query": query,
                    "result_count": len(recalled_bundle.get("results", [])) if isinstance(recalled_bundle.get("results"), list) else 0,
                },
                relationships={"source_node_ids": query_node_ids},
            )
            if artifact_id:
                artifact_ids.append(artifact_id)
        return artifact_ids, recalled_node_ids

    def _persist_artifact(
        self,
        *,
        artifact_name: str,
        parent_id: Optional[str],
        title: str,
        content_text: str,
        metadata: Optional[Dict[str, Any]] = None,
        relationships: Optional[Dict[str, List[str]]] = None,
    ) -> Optional[str]:
        """Store an audit artifact in the hierarchy without indexing it for search."""
        json_source = getattr(self.orchestrator, "_json_source", None)
        if json_source is None or not parent_id:
            return None

        artifact_node_id = f"node_{uuid.uuid4().hex}"
        node = Node(
            node_id=artifact_node_id,
            name=title,
            parent_id=parent_id,
            payload={"text": content_text},
            metadata=metadata or {},
            relationships=relationships or {},
        )
        # Audit artifacts should remain inspectable in Redis without polluting the recall index.
        json_source.upsert_node(node, record_status="ready", last_error="")
        return artifact_node_id

    def _create_workspace_artifact(
        self,
        name: str,
        parent_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create a non-indexed workspace node for organizing a research run."""
        json_source = getattr(self.orchestrator, "_json_source", None)
        if json_source is None:
            return None

        workspace_node_id = f"workspace_{uuid.uuid4().hex}"
        json_source.upsert_node(
            Node(
                node_id=workspace_node_id,
                name=name,
                parent_id=parent_id,
                metadata=metadata or {},
            ),
            record_status="ready",
            last_error="",
        )
        return workspace_node_id

    def _collect_returned_node_ids(
        self,
        tool_calls: Sequence[Any],
        tool_name: str,
    ) -> List[str]:
        """Collect returned node identifiers for a specific tool name."""
        return [trace.returned_node_id for trace in tool_calls if trace.tool_name == tool_name and trace.succeeded and trace.returned_node_id]

    def _collect_recalled_node_ids(self, recalled_memories: List[Dict[str, Any]]) -> set[str]:
        """Extract recalled node identifiers from aggregated search results."""
        node_ids: set[str] = set()
        for recalled_bundle in recalled_memories:
            results = recalled_bundle.get("results", [])
            if not isinstance(results, list):
                continue
            for result in results:
                if isinstance(result, dict):
                    node_id = result.get("node_id")
                    if isinstance(node_id, str) and node_id:
                        node_ids.add(node_id)
        return node_ids

    def _append_check(
        self,
        checks: List[VerificationCheck],
        *,
        name: str,
        passed: bool,
        failure_details: str,
        success_details: str,
    ) -> None:
        """Append a pass or fail verification result."""
        checks.append(
            VerificationCheck(
                name=name,
                status="pass" if passed else "fail",
                details=success_details if passed else failure_details,
            )
        )

    def _valid_final_queries(self, plan: Dict[str, Any]) -> List[str]:
        """Normalize valid final recall queries from the structured plan."""
        queries = plan.get("final_search_queries", [])
        if not isinstance(queries, list):
            return []
        return [query.strip() for query in queries if isinstance(query, str) and query.strip()]

    def _plan_topic_title(self, plan: Dict[str, Any]) -> str:
        """Return a slug-friendly topic title from the plan when available."""
        topic_title = plan.get("topic_title", "")
        return topic_title if isinstance(topic_title, str) else ""

    def _slugify(self, value: str) -> str:
        """Convert free text into a filesystem-like slug for workspace naming."""
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
        return normalized[:40]

    def _timestamp(self) -> str:
        """Return a stable UTC timestamp string for audit metadata."""
        return datetime.now(timezone.utc).isoformat()

    def _normalize_optional_id(self, value: Optional[str]) -> Optional[str]:
        """Treat blank identifier strings as missing values."""
        return value or None

    def _handle_executor_progress(self, event: ProgressEvent) -> None:
        """Forward executor progress while attaching the active run context."""
        self._emit_progress(
            stage=event.stage,
            status=event.status,
            message=event.message,
            run_id=self._active_run_id,
            step_number=event.step_number,
            tool_name=event.tool_name,
            data=event.data,
        )

    def _emit_progress(
        self,
        *,
        stage: str,
        status: str,
        message: str,
        run_id: Optional[str] = None,
        step_number: Optional[int] = None,
        tool_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a structured progress event when a listener is configured."""
        if self.on_progress is None:
            return
        self.on_progress(
            ProgressEvent(
                stage=stage,
                status=status,
                message=message,
                run_id=run_id,
                step_number=step_number,
                tool_name=tool_name,
                data=data or {},
            )
        )
