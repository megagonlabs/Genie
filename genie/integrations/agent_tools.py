"""Reusable agent-tool helpers for exposing Genie through LLM tool calling.

This module intentionally lives outside the core orchestration layer. The
``MemoryOrchestrator`` owns the business operations, while this integration
module translates those operations into portable JSON-schema tool definitions
and callable registries that agent frameworks can consume.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence


ToolCallable = Callable[..., Any]
ToolSchema = Dict[str, Any]

DEFAULT_MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "create_workspace",
    "save_memory",
    "search_memory",
)

_MEMORY_TOOL_SCHEMAS: Dict[str, ToolSchema] = {
    "create_workspace": {
        "type": "function",
        "function": {
            "name": "create_workspace",
            "description": (
                "Create a workspace node to organize hierarchical memory. "
                "Use this to build reusable folders before saving findings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Human-readable workspace title such as "
                            "'market_landscape', 'evidence', or 'report_outline'."
                        ),
                    },
                    "parent_id": {
                        "type": "string",
                        "description": (
                            "Optional exact parent node_id returned by Genie. "
                            "Omit or pass null for a root workspace."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    "save_memory": {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a concrete finding, note, source summary, decision, or draft paragraph "
                "inside the most relevant Genie workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_node_id": {
                        "type": "string",
                        "description": "Exact workspace node_id where the memory should be stored.",
                    },
                    "content_text": {
                        "type": "string",
                        "description": "The text content to store for later recall and synthesis.",
                    },
                    "name": {
                        "type": "string",
                        "description": "A short descriptive label for the saved memory node.",
                    },
                },
                "required": ["workspace_node_id", "content_text", "name"],
            },
        },
    },
    "search_memory": {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search previously saved Genie memories semantically and lexically. "
                "Use this to recover relevant findings before reasoning or synthesis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "scope_node_id": {
                        "type": "string",
                        "description": (
                            "Optional exact workspace node_id used to restrict results to a subtree."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query_text"],
            },
        },
    },
}


def get_memory_tool_schemas(tool_names: Sequence[str] | None = None) -> List[ToolSchema]:
    """Return portable JSON-schema tool definitions for Genie memory operations.

    Parameters:
    tool_names:
        Optional ordered subset of tool names to expose. When omitted, the full
        default Genie memory tool set is returned.

    Returns:
    list[dict[str, Any]]
        Deep-copied tool schema definitions suitable for LLM tool-calling APIs.
    """
    selected_names = _normalize_tool_names(tool_names)
    return [deepcopy(_MEMORY_TOOL_SCHEMAS[tool_name]) for tool_name in selected_names]


def build_memory_tool_registry(
    orchestrator: Any,
    tool_names: Iterable[str] | None = None,
) -> Dict[str, ToolCallable]:
    """Build a callable registry for the selected Genie memory operations.

    Parameters:
    orchestrator:
        Any object exposing Genie-compatible methods such as ``create_workspace``,
        ``save_memory``, and ``search_memory``.
    tool_names:
        Optional ordered subset of tool names to expose. When omitted, the full
        default Genie memory tool set is returned.

    Returns:
    dict[str, Callable[..., Any]]
        Mapping from tool names to bound callables on the orchestrator.
    """
    selected_names = _normalize_tool_names(tool_names)
    registry: Dict[str, ToolCallable] = {}
    for tool_name in selected_names:
        try:
            registry[tool_name] = getattr(orchestrator, tool_name)
        except AttributeError as exc:
            raise AttributeError(
                f"Orchestrator does not expose required memory tool method '{tool_name}'."
            ) from exc
    return registry


def _normalize_tool_names(tool_names: Sequence[str] | Iterable[str] | None) -> List[str]:
    """Validate and normalize an ordered list of Genie tool names."""
    if tool_names is None:
        return list(DEFAULT_MEMORY_TOOL_NAMES)

    normalized_names = list(tool_names)
    if not normalized_names:
        raise ValueError("tool_names must contain at least one Genie tool name.")

    unknown_names = [tool_name for tool_name in normalized_names if tool_name not in _MEMORY_TOOL_SCHEMAS]
    if unknown_names:
        raise ValueError(
            "Unknown Genie tool name(s): "
            + ", ".join(sorted(set(unknown_names)))
        )
    return normalized_names
