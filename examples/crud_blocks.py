"""Create, read, update, and delete memory blocks.

Run from the repository root:

    conda run -n "genie" python examples/crud_blocks.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import Memory


def main() -> None:
    memory = Memory(principal="agent:demo")

    project = memory.put(type="project", value={"name": "Launch notes"})
    note = memory.put(
        parent=project,
        type="note",
        value={"body": "Draft the first outline", "status": "open"},
        metadata={"priority": 2},
    )

    patched = memory.patch(note, value={"status": "review"}, metadata={"priority": 3})
    replaced = memory.set(patched, {"body": "Outline reviewed", "status": "done"})

    print(f"created project: {project.id}")
    print(f"updated note v{replaced.version}: {replaced.value}")
    print(f"project children: {[child.type for child in memory.children(project)]}")

    memory.delete(note)
    assert memory.get(note.id) is None
    assert memory.children(project) == []


if __name__ == "__main__":
    main()
