"""End-to-end Genie walkthrough.

This example combines the main concepts shown in the smaller examples:

- a full `Memory` object kept at the application boundary
- scoped memory handles passed to agents
- hierarchical blocks for project/task/note structure
- text search, metadata filters, and linked-memory search
- typed links between memory blocks
- policies for principal-specific access control
- hooks, dataclass codecs, and stdlib codecs as extension points

Run from the repository root:

    conda run -n "genie" python examples/basic_usage.py
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
from typing import Any
import uuid

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    AccessDenied,
    AccessRule,
    CodecRegistry,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryBlock,
    MemoryHook,
    MemoryPolicy,
    MemoryScope,
    SearchResult,
)
from genie.codecs.stdlib import (
    DataclassCodec,
    DatetimeCodec,
    DecimalCodec,
    UUIDCodec,
)


@dataclass(frozen=True)
class StylePreference:
    """Custom preference value used by the integrated walkthrough.

    Attributes:
        tone: Desired writing tone.
        length: Desired response length.
    """

    tone: str
    length: str


class AuditHook(MemoryHook):
    """Hook that records creation and deletion events."""

    def __init__(self) -> None:
        """Create an empty audit hook."""

        self.events: list[str] = []

    def after_put(self, block: MemoryBlock) -> None:
        """Record a block creation event."""

        self.events.append(f"created:{block.type}:{block.id}")

    def after_delete(self, block: MemoryBlock) -> None:
        """Record a block deletion event."""

        self.events.append(f"deleted:{block.type}:{block.id}")


@dataclass
class Agent:
    """Small example agent that depends only on `MemoryScope`.

    Attributes:
        name: Agent name used in printed output.
        memory: Scoped memory handle assigned by the application.
    """

    name: str
    memory: MemoryScope

    def remember(self, type: str, value: object, **metadata: object) -> MemoryBlock:
        """Write a block into this agent's scoped memory.

        Args:
            type: Block type.
            value: Block value.
            **metadata: Metadata fields to attach.

        Returns:
            Created memory block.
        """

        block = self.memory.put(type=type, value=value, metadata=metadata)
        print(f"{self.name} wrote {block.type}: {block.value}")
        return block

    def recall(self, text: str, **search_options: Any) -> list[MemoryBlock]:
        """Search this agent's scoped memory.

        Args:
            text: Text query.
            **search_options: Additional options passed to `MemoryScope.search`.

        Returns:
            Matching memory blocks.
        """

        results = self.memory.search(text, **search_options)
        blocks = [result.block for result in results]
        print(f"{self.name} recalled {len(blocks)} block(s) for {text!r}")
        return blocks


def main() -> None:
    """Run the integrated Genie walkthrough."""

    audit = AuditHook()
    memory = Memory(
        backend=InMemoryBackend(),
        principal="agent:root",
        hooks=[audit],
        codecs=CodecRegistry(
            [
                DatetimeCodec(),
                DecimalCodec(),
                UUIDCodec(),
                DataclassCodec([StylePreference]),
                JsonCodec(),
            ]
        ),
    )

    section("Seed application-owned memory")
    request_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    project = memory.put(
        type="project",
        value={"name": "Proposal draft", "deadline": "Friday"},
        metadata={"workspace": "demo", "request_id": str(request_id)},
    )
    memory.put(parent=project, type="request_id", value=request_id)
    due_at = memory.put(
        parent=project,
        type="deadline",
        value=dt.datetime(2026, 7, 3, 17, 0, tzinfo=dt.timezone.utc),
    )
    budget = memory.put(parent=project, type="budget", value=Decimal("1250.75"))
    requirement = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database-agnostic.",
        metadata={"priority": 3, "owner": "team"},
    )
    style = memory.put(
        parent=project,
        type="preference",
        value=StylePreference(tone="warm", length="short"),
    )

    section("Pass scopes to agents")
    planner = Agent(
        name="planner",
        memory=memory.scope(root=project, principal="agent:planner", allow_up=True),
    )
    task = planner.remember(
        "task",
        {"title": "Draft outline", "status": "open"},
        assignee="agent:writer",
        priority=2,
    )

    writer = Agent(
        name="writer",
        memory=memory.scope(root=task, principal="agent:writer", allow_up=True),
    )
    outline = writer.remember(
        "note",
        "Use three sections: problem, approach, next steps.",
        source="draft",
    )

    section("Search inside the scoped hierarchy")
    local_results = writer.recall("sections")
    assert [block.id for block in local_results] == [outline.id]

    ancestor_results = writer.recall("draft", scope="ancestors")
    assert task.id in {block.id for block in ancestor_results}

    filtered = planner.memory.search(
        "concise",
        types=["requirement"],
        filters={"metadata.priority": {">=": 3}},
    )
    print_results(filtered)
    assert [result.block.id for result in filtered] == [requirement.id]

    section("Add a typed link and search linked memory")
    memory.add_link(requirement, outline, "informs", metadata={"confidence": 0.9})
    linked = memory.search("sections", root=requirement, scope="links")
    print_results(linked)
    assert [result.block.id for result in linked] == [outline.id]

    section("Use policies for private working memory")
    private_note = planner.memory.put(
        parent=task,
        type="private_note",
        value="Planner-only concern: trim examples if the draft grows.",
        policy=MemoryPolicy(read=AccessRule.only(principals={"agent:planner"})),
    )
    assert planner.memory.get(private_note) is not None
    try:
        writer.memory.get(private_note)
    except AccessDenied as exc:
        print(f"blocked private note read: {exc}")

    section("Patch data and decode typed values")
    updated_task = writer.memory.patch(task, value={"status": "in_progress"})
    decoded_style = planner.memory.get(style)
    decoded_due_at = planner.memory.get(due_at)
    decoded_budget = planner.memory.get(budget)
    assert decoded_style is not None
    assert decoded_due_at is not None
    assert decoded_budget is not None
    print(f"updated task: {updated_task.value}")
    print(f"decoded style preference: {decoded_style.value}")
    print(f"decoded deadline: {decoded_due_at.value.isoformat()}")
    print(f"decoded budget: {decoded_budget.value}")
    assert updated_task.value["status"] == "in_progress"
    assert decoded_style.value == StylePreference(tone="warm", length="short")
    assert decoded_due_at.value == dt.datetime(
        2026,
        7,
        3,
        17,
        0,
        tzinfo=dt.timezone.utc,
    )
    assert decoded_budget.value == Decimal("1250.75")

    section("Scopes prevent accidental escape")
    other_project = memory.put(type="project", value={"name": "Other project"})
    try:
        writer.memory.get(style)
    except AccessDenied as exc:
        print(f"blocked sibling read: {exc}")
    try:
        planner.memory.scope(root=other_project)
    except AccessDenied as exc:
        print(f"blocked out-of-scope subscope: {exc}")

    section("Audit hook observed writes")
    print(f"audit events: {len(audit.events)}")
    assert any(event.startswith("created:task:") for event in audit.events)

    section("Done")
    print("The full Memory object stayed at the boundary; agents only received scopes.")


def section(title: str) -> None:
    """Print a section heading.

    Args:
        title: Heading text.
    """

    print(f"\n== {title} ==")


def print_results(results: list[SearchResult]) -> None:
    """Print search results in a compact form.

    Args:
        results: Search results to print.
    """

    for result in results:
        block = result.block
        print(f"- {block.type} {block.id}: {block.value}")


if __name__ == "__main__":
    main()
