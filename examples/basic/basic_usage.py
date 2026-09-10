"""End-to-end Genie walkthrough.

This example combines the main concepts shown in the smaller examples:

- a full `Memory` object kept in trusted application code
- bounded memory handles passed to agents
- parent/child ownership for project/task/note structure
- text search, metadata filters, and linked-memory search
- typed links between memory blocks
- policies for principal-specific access control
- hooks, dataclass codecs, and stdlib codecs as ways to customize Genie

Run from the repository root:

    python examples/basic/basic_usage.py
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    AccessDenied,
    CodecRegistry,
    HookContext,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryBlock,
    MemoryHandle,
    MemoryHook,
    MemoryPolicy,
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

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a block creation event."""

        self.events.append(f"created:{block.type}:{block.id}")

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a block deletion event."""

        self.events.append(f"deleted:{block.type}:{block.id}")


@dataclass
class Agent:
    """Small example agent that depends only on `MemoryHandle`.

    Attributes:
        name: Agent name used in printed output.
        memory: Bounded memory handle assigned by the application.
    """

    name: str
    memory: MemoryHandle

    def remember(
        self,
        type: str,
        value: object,
        *,
        policy: MemoryPolicy | None = None,
        **metadata: object,
    ) -> MemoryBlock:
        """Write a block through this agent's memory handle.

        Args:
            type: Block type.
            value: Block value.
            policy: Optional access policy for the new block.
            **metadata: Metadata fields to attach.

        Returns:
            Created memory block.
        """

        block = self.memory.put(type=type, value=value, metadata=metadata, policy=policy)
        print(f"{self.name} wrote {block.type}: {block.value}")
        return block

    def recall(self, text: str, **search_options: Any) -> list[MemoryBlock]:
        """Search through this agent's memory handle.

        Args:
            text: Text query.
            **search_options: Additional options passed to `MemoryHandle.search`.

        Returns:
            Matching memory blocks.
        """

        results = self.memory.search(text, **search_options)
        blocks = [result.block for result in results]
        print(f"{self.name} recalled {len(blocks)} block(s) for {text!r}")
        for block in blocks:
            print(f"- {block.type}: {block.value}")
        return blocks


def main() -> None:
    """Run the integrated Genie walkthrough."""

    print("== Integrated agent memory walkthrough ==")
    audit = AuditHook()
    team_policy = MemoryPolicy.owner_or(principals={"agent:root", "agent:planner", "agent:writer"})
    planner_policy = MemoryPolicy.owner_or(principals={"agent:planner"})
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
        policy=team_policy,
    )
    memory.put(parent=project, type="request_id", value=request_id, policy=team_policy)
    due_at = memory.put(
        parent=project,
        type="deadline",
        value=dt.datetime(2026, 7, 3, 17, 0, tzinfo=dt.UTC),
        policy=team_policy,
    )
    budget = memory.put(parent=project, type="budget", value=Decimal("1250.75"), policy=team_policy)
    requirement = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database-agnostic.",
        metadata={"priority": 3, "owner": "team"},
        policy=team_policy,
    )
    style = memory.put(
        parent=project,
        type="preference",
        value=StylePreference(tone="warm", length="short"),
        policy=team_policy,
    )

    section("Pass handles to agents")
    planner = Agent(
        name="planner",
        memory=memory.get_handle(
            root=project,
            principal="agent:planner",
            allow_up=True,
            allow_policy_override=True,
        ),
    )
    task = planner.remember(
        "task",
        {"title": "Draft outline", "status": "open"},
        policy=team_policy,
        assignee="agent:writer",
        priority=2,
    )

    writer = Agent(
        name="writer",
        memory=memory.get_handle(
            root=task,
            principal="agent:writer",
            allow_up=True,
            allow_policy_override=True,
        ),
    )
    outline = writer.remember(
        "note",
        "Use three sections: problem, approach, next steps.",
        policy=team_policy,
        source="draft",
    )

    section("Search inside the bounded ownership tree")
    local_results = writer.recall("sections")
    assert [block.id for block in local_results] == [outline.id]

    ancestor_results = writer.recall("draft", scope="ancestors")
    assert task.id in {block.id for block in ancestor_results}

    filtered = planner.memory.search(
        "concise",
        types=["requirement"],
        filters={"metadata.priority": {">=": 3}},
    )
    print("planner's filtered search results:")
    print_results(filtered)
    assert [result.block.id for result in filtered] == [requirement.id]

    section("Add a typed link and search linked memory")
    memory.add_link(requirement, outline, "informs", metadata={"confidence": 0.9})
    linked = memory.search("sections", root=requirement, scope="links")
    print("linked search results:")
    print_results(linked)
    assert [result.block.id for result in linked] == [outline.id]

    section("Use policies for private working memory")
    private_note = planner.memory.put(
        parent=task,
        type="private_note",
        value="Planner-only concern: trim examples if the draft grows.",
        policy=planner_policy,
    )
    assert planner.memory.get(private_note) is not None
    try:
        writer.memory.get(private_note)
    except AccessDenied:
        print("writer reading planner-private note: blocked as expected")
    else:
        raise AssertionError("The writer must not read the planner's private note.")

    section("Update data and decode typed values")
    updated_task = writer.memory.update(task, value={"status": "in_progress"})
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
        tzinfo=dt.UTC,
    )
    assert decoded_budget.value == Decimal("1250.75")

    section("Scopes prevent accidental escape")
    other_project = memory.put(type="project", value={"name": "Other project"})
    try:
        writer.memory.get(style)
    except AccessDenied:
        print("writer reading outside its task subtree: blocked as expected")
    else:
        raise AssertionError("The writer must not read outside its task subtree.")
    try:
        planner.memory.get_handle(root=other_project)
    except AccessDenied:
        print("planner creating a handle outside its project: blocked as expected")
    else:
        raise AssertionError("The planner must not create an out-of-scope handle.")

    section("Audit hook observed writes and deletions")
    memory.delete(other_project)
    print(f"audit events: {len(audit.events)}")
    assert any(event.startswith("created:task:") for event in audit.events)
    assert f"deleted:project:{other_project.id}" in audit.events

    section("Done")
    print("The full Memory object stayed in trusted application code; agents only received handles.")


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
        print(f"- {block.type}: {block.value}")


if __name__ == "__main__":
    main()
