"""Create, read, update, and delete memory blocks.

Run from the repository root:

    conda run -n "genie" python examples/basic/crud_blocks.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory


def main() -> None:
    """Run a minimal CRUD walkthrough."""

    print("== CRUD and ownership ==")
    memory = Memory(backend=InMemoryBackend(), principal="agent:demo")

    project = memory.put(type="project", value={"name": "Launch notes"})
    note = memory.put(
        parent=project,
        type="note",
        value={"body": "Draft the first outline", "status": "open"},
        metadata={"priority": 2},
    )

    updated = memory.update(note, value={"status": "review"}, metadata={"priority": 3})
    replaced = memory.set(updated, {"body": "Outline reviewed", "status": "done"})

    print(f"created project: {project.value['name']}")
    print(f"updated note (version {replaced.version}): {replaced.value}")
    print(f"children before delete: {[child.type for child in memory.children(project)]}")

    memory.delete(note)
    assert memory.get(note.id) is None
    assert memory.children(project) == []
    print("children after delete: []")


if __name__ == "__main__":
    main()
