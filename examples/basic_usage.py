"""End-to-end Genie walkthrough.

This example combines the main concepts shown in the smaller examples:

- a full `Memory` object kept at the application boundary
- scoped memory handles passed to agents
- hierarchical blocks for project/task/note structure
- text search, metadata filters, and linked-memory search
- typed links between memory blocks
- policies for principal-specific access control
- hooks and codecs as extension points

Run from the repository root:

    conda run -n "genie" python examples/basic_usage.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    AccessDenied,
    AccessRule,
    CodecRegistry,
    EncodedValue,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryBlock,
    MemoryCodec,
    MemoryHook,
    MemoryPolicy,
    MemoryScope,
    SearchResult,
)


@dataclass(frozen=True)
class StylePreference:
    tone: str
    length: str


class StylePreferenceCodec(MemoryCodec):
    content_type = "application/vnd.genie.style-preference+json"

    def can_encode(self, value: Any) -> bool:
        return isinstance(value, StylePreference)

    def encode(self, value: Any) -> EncodedValue:
        return EncodedValue(
            self.content_type,
            {"tone": value.tone, "length": value.length},
        )

    def decode(self, value: EncodedValue) -> Any:
        return StylePreference(**value.data)


class AuditHook(MemoryHook):
    def __init__(self) -> None:
        self.events: list[str] = []

    def after_put(self, block: MemoryBlock) -> None:
        self.events.append(f"created:{block.type}:{block.id}")

    def after_delete(self, block: MemoryBlock) -> None:
        self.events.append(f"deleted:{block.type}:{block.id}")


@dataclass
class Agent:
    name: str
    memory: MemoryScope

    def remember(self, type: str, value: object, **metadata: object) -> MemoryBlock:
        block = self.memory.put(type=type, value=value, metadata=metadata)
        print(f"{self.name} wrote {block.type}: {block.value}")
        return block

    def recall(self, text: str, **search_options: Any) -> list[MemoryBlock]:
        results = self.memory.search(text, **search_options)
        blocks = [result.block for result in results]
        print(f"{self.name} recalled {len(blocks)} block(s) for {text!r}")
        return blocks


def main() -> None:
    audit = AuditHook()
    memory = Memory(
        backend=InMemoryBackend(),
        principal="agent:root",
        hooks=[audit],
        codecs=CodecRegistry([JsonCodec(), StylePreferenceCodec()]),
    )

    section("Seed application-owned memory")
    project = memory.put(
        type="project",
        value={"name": "Proposal draft", "deadline": "Friday"},
        metadata={"workspace": "demo"},
    )
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

    section("Patch data and decode custom values")
    updated_task = writer.memory.patch(task, value={"status": "in_progress"})
    decoded_style = planner.memory.get(style)
    assert decoded_style is not None
    print(f"updated task: {updated_task.value}")
    print(f"decoded style preference: {decoded_style.value}")
    assert updated_task.value["status"] == "in_progress"
    assert decoded_style.value == StylePreference(tone="warm", length="short")

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
    print(f"\n== {title} ==")


def print_results(results: list[SearchResult]) -> None:
    for result in results:
        block = result.block
        print(f"- {block.type} {block.id}: {block.value}")


if __name__ == "__main__":
    main()
