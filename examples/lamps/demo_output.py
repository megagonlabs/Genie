"""Shared terminal renderer for runnable lamp agents."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Protocol


class ToolView(Protocol):
    """Minimum tool-definition fields that the renderer requires."""

    @property
    def name(self) -> str:
        """Return the tool name."""

        ...

    @property
    def description(self) -> str:
        """Return the tool description that the user sees."""

        ...


class ToolCallView(Protocol):
    """Minimum tool-call fields that the renderer requires."""

    @property
    def name(self) -> str:
        """Return the name of the called tool."""

        ...


def print_agent_demo(
    *,
    title: str,
    purpose: str,
    structures: Sequence[str],
    tools: Iterable[ToolView],
    answer: str,
    calls: Sequence[ToolCallView],
) -> None:
    """Print a lamp agent demo in the common terminal format, with a short introduction.

    Args:
        title: Name of the example that the output shows to the user.
        purpose: One-sentence explanation of the agent workflow.
        structures: Genie structure facades that the example uses.
        tools: Tool definitions that the agent receives.
        answer: Final readable answer that the agent assembles.
        calls: Tool-call records in call order, which the reader can inspect.
    """

    print(f"== {title} ==")
    print(purpose)
    print(f"Genie structures: {', '.join(structures)}\n")

    print("Tools available to the agent:")
    for tool in tools:
        print(f"- {tool.name}: {tool.description}")

    print(f"\nAgent answer:\n{answer}")

    print("\nInspectable tool calls:")
    for index, call in enumerate(calls, start=1):
        print(f"{index:02d}. {call.name}")

    print("\nTool usage:")
    for name, count in Counter(call.name for call in calls).items():
        print(f"- {name} x{count}")
