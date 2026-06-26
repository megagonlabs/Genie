"""Compatibility wrapper around the reusable Genie agent-tool integration API."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable

from genie.integrations.agent_tools import (
    build_memory_tool_registry as build_genie_memory_tool_registry,
    get_memory_tool_schemas,
)


# Keep the demo app import surface stable while sourcing the actual schemas
# from the reusable Genie integration module.
MEMORY_TOOLS = get_memory_tool_schemas()


def build_memory_tool_registry(
    orchestrator: Any,
    tool_names: Iterable[str] | None = None,
) -> Dict[str, Callable[..., Any]]:
    """Build the callable tool registry for the demo agent.

    Parameters:
    orchestrator:
        Genie-compatible orchestrator exposing the selected memory methods.
    tool_names:
        Optional ordered subset of Genie tool names to expose.

    Returns:
    dict[str, Callable[..., Any]]
        Mapping from tool names to bound orchestrator methods.
    """
    return build_genie_memory_tool_registry(orchestrator, tool_names=tool_names)
