"""Data models for deep-research execution traces and audit verification."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ToolCallTrace(BaseModel):
    """Structured record of a single tool call inside the execution loop.

    Parameters:
    step_number:
        Sequential tool-call number within the run.
    tool_call_id:
        Provider-specific identifier for the tool call.
    tool_name:
        Name of the invoked tool.
    arguments:
        Parsed JSON arguments supplied by the model.
    result:
        Decoded tool result when available.
    response_text:
        Raw tool response text that was returned to the model.
    succeeded:
        Whether the tool executed successfully.
    error_message:
        Optional error text when the tool failed.
    returned_node_id:
        Node identifier returned by the tool, when applicable.
    """

    step_number: int
    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    response_text: str
    succeeded: bool
    error_message: Optional[str] = None
    returned_node_id: Optional[str] = None


class ToolLoopResult(BaseModel):
    """Complete execution trace returned by the tool loop.

    Parameters:
    final_output:
        Final assistant text returned after tool usage completed.
    tool_calls:
        Ordered list of tool-call trace records.
    assistant_turn_count:
        Number of assistant completion turns used in the loop.
    """

    final_output: str
    tool_calls: List[ToolCallTrace] = Field(default_factory=list)
    assistant_turn_count: int


class VerificationCheck(BaseModel):
    """Single verification rule outcome for a deep-research run.

    Parameters:
    name:
        Short stable identifier for the check.
    status:
        One of ``pass``, ``warn``, or ``fail``.
    details:
        Human-readable explanation of the outcome.
    """

    name: str
    status: str
    details: str


class ResearchRunVerification(BaseModel):
    """Aggregate verification summary for a completed deep-research run.

    Parameters:
    run_id:
        Stable identifier for the run.
    status:
        Overall verdict for the run.
    checks:
        Individual verification outcomes.
    metrics:
        Computed run metrics such as tool counts and node overlap.
    warnings:
        Additional non-fatal concerns detected during verification.
    audit_root_node_id:
        Optional Redis node id that anchors the audit subtree.
    research_root_node_id:
        Optional Redis node id that anchors the run's research subtree.
    artifact_node_ids:
        Mapping of audit artifact names to stored node ids.
    """

    run_id: str
    status: str
    checks: List[VerificationCheck] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    audit_root_node_id: Optional[str] = None
    research_root_node_id: Optional[str] = None
    artifact_node_ids: Dict[str, str] = Field(default_factory=dict)


class ResearchRunResult(BaseModel):
    """Full result bundle for a deep-research execution.

    Parameters:
    run_id:
        Stable identifier for the run.
    final_report:
        Final synthesized report text.
    plan:
        Structured plan returned by the planning stage.
    execution_summary:
        Summary emitted by the execution tool loop.
    recalled_memories:
        Search results recalled before synthesis.
    execution_trace:
        Detailed tool-call trace from the execution stage.
    verification:
        Verification verdict and metrics for the run.
    audit_root_node_id:
        Optional Redis node id for the audit subtree root.
    research_root_node_id:
        Optional Redis node id for the research subtree root.
    """

    run_id: str
    final_report: str
    plan: Dict[str, Any] = Field(default_factory=dict)
    execution_summary: str
    recalled_memories: List[Dict[str, Any]] = Field(default_factory=list)
    execution_trace: ToolLoopResult
    verification: ResearchRunVerification
    audit_root_node_id: Optional[str] = None
    research_root_node_id: Optional[str] = None
