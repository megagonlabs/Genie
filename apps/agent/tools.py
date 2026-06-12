"""Tool schemas and helper registry builders for agent workflows."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping


MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_workspace",
            "description": (
                "Create a workspace node to organize the research hierarchy. "
                "Use this before saving memories so the work is grouped into logical folders."
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
                            "Exact parent node_id returned by Genie. Omit or pass null for a root workspace."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save a concrete finding, note, source summary, decision, or draft paragraph "
                "inside the most relevant workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_node_id": {
                        "type": "string",
                        "description": "Exact workspace node_id where the note should be stored.",
                    },
                    "content_text": {
                        "type": "string",
                        "description": "The content to store for later recall and synthesis.",
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
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search previously saved memories semantically. Use this before writing the "
                "final answer and whenever you need to recover prior findings."
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
                            "Optional exact workspace node_id to restrict results to a subtree."
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
]


def build_memory_tool_registry(orchestrator: Any) -> Dict[str, Callable[..., Any]]:
    """Build the callable tool registry for a Genie-backed memory agent.

    Parameters:
    orchestrator:
        Memory orchestrator instance exposing the supported tool methods.

    Returns:
    dict[str, Callable[..., Any]]
        Mapping from tool names to bound orchestrator methods.
    """
    return {
        "create_workspace": orchestrator.create_workspace,
        "save_memory": orchestrator.save_memory,
        "search_memory": orchestrator.search_memory,
    }
