"""Shared terminal renderer for runnable lamp agents."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Protocol


class ToolView(Protocol):
    """Minimum tool-definition fields required by the renderer."""

    @property
    def name(self) -> str:
        """Return the tool name."""

        ...

    @property
    def description(self) -> str:
        """Return the user-facing tool description."""

        ...


class ToolCallView(Protocol):
    """Minimum tool-call fields required by the renderer."""

    @property
    def name(self) -> str:
        """Return the invoked tool name."""

        ...


def print_agent_demo(
    *,
    tools: Iterable[ToolView],
    answer: str,
    calls: Sequence[ToolCallView],
) -> None:
    """Print a lamp agent using the common four-section terminal format.

    Args:
        tools: Tool definitions presented to the agent.
        answer: Final readable answer assembled by the agent.
        calls: Ordered, inspectable tool-call records.
    """

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
