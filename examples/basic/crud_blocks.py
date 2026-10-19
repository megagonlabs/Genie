"""Create, read, update, traverse, and recursively delete memory blocks.

Run from the repository root:

    python examples/basic/crud_blocks.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory


def main() -> None:
    """Run a short CRUD walkthrough."""

    print("== CRUD and ownership ==")
    memory = Memory(backend=InMemoryBackend(), principal="agent:demo")

    project = memory.put(type="project", value={"name": "Launch notes"})
    note = memory.put(
        parent=project,
        type="note",
        value={"body": "Draft the first outline", "status": "open"},
        metadata={"priority": 2},
    )
    checklist = memory.put(
        parent=note,
        type="checklist",
        value=["draft", "review"],
    )

    updated = memory.update(note, value={"status": "review"}, metadata={"priority": 3})
    replaced = memory.set(updated, {"body": "Outline reviewed", "status": "done"})
    retrieved = memory.get(replaced.id)
    ancestors = memory.ancestors(checklist)

    print(f"created project: {project.value['name']}")
    print(f"updated note (version {replaced.version}): {replaced.value}")
    print(f"children before delete: {[child.type for child in memory.children(project)]}")
    print(f"checklist ancestors: {[block.type for block in ancestors]}")

    assert retrieved == replaced
    assert replaced.version == note.version + 2
    assert [block.id for block in ancestors] == [note.id, project.id]

    # A parent owns its children. So when `Memory` deletes the project, it also
    # deletes the note and the checklist recursively.
    memory.delete(project)
    assert memory.get(project.id) is None
    assert memory.get(note.id) is None
    assert memory.get(checklist.id) is None
    print("deleting the project removed its owned descendants")


if __name__ == "__main__":
    main()
