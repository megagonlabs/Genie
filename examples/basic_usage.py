"""Developer-friendly Genie walkthrough.

Run from the repository root:

    conda run -n "genie" python -m examples.basic_usage
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genie import AccessDenied, InMemoryBackend, Memory, MemoryBlock, MemoryScope, SearchResult


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
    memory = Memory(backend=InMemoryBackend(), principal="agent:root")

    project = memory.put(
        type="project",
        value={"name": "Proposal draft", "deadline": "Friday"},
        metadata={"workspace": "demo"},
    )
    requirement = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database-agnostic.",
        metadata={"priority": 3},
    )
    preference = memory.put(
        parent=project,
        type="preference",
        value={"style": "concise", "tone": "warm"},
    )

    print_header("Top-level agent gets a project memory scope")
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

    print_header("Sub-agent gets a narrower task memory scope")
    writer = Agent(
        name="writer",
        memory=planner.memory.scope(root=task, allow_up=True),
    )
    outline = writer.remember(
        "note",
        "Use three sections: problem, approach, next steps.",
        source="draft",
    )

    local_results = writer.recall("sections")
    assert [block.id for block in local_results] == [outline.id]

    ancestor_results = writer.recall("draft", scope="ancestors")
    assert task.id in {block.id for block in ancestor_results}

    print_header("Search can combine text, type, metadata, and scope")
    filtered = planner.memory.search(
        "concise",
        types=["requirement"],
        filters={"metadata.priority": {">=": 3}},
    )
    print_blocks(filtered)
    assert [result.block.id for result in filtered] == [requirement.id]

    print_header("Links model cross-memory relationships")
    memory.link(requirement, outline, "informs")
    linked = memory.search("sections", root=requirement, scope="links")
    print_blocks(linked)
    assert [result.block.id for result in linked] == [outline.id]

    print_header("Blocks can be patched or replaced")
    updated_task = writer.memory.patch(task, value={"status": "in_progress"})
    print(f"updated task: {updated_task.value}")
    assert updated_task.value["status"] == "in_progress"

    print_header("Memory scopes prevent accidental escape")
    other_project = memory.put(type="project", value={"name": "Other project"})
    try:
        writer.memory.get(preference)
    except AccessDenied as exc:
        print(f"blocked sibling read: {exc}")
    try:
        planner.memory.scope(root=other_project)
    except AccessDenied as exc:
        print(f"blocked out-of-scope subscope: {exc}")

    print_header("Done")
    print("The full Memory object stayed at the boundary; agents only received scopes.")


def print_header(title: str) -> None:
    print(f"\n== {title} ==")


def print_blocks(results: list[SearchResult]) -> None:
    for result in results:
        block = result.block
        print(f"- {block.type} {block.id}: {block.value}")


if __name__ == "__main__":
    main()
