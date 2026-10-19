"""Test subtree membership, and show how a reportable absence differs from a real error.

Genie has 2 kinds of absence:

- **Absence as an answer:** a read of a block that is not there returns a
  value. `get` returns `None`, and `contains` returns `False`.
- **Absence as an error:** an operation that acts on a specific block cannot
  report the absence as a value. So it raises `MemoryNotFound`.

Run from the repository root:

    python examples/basic/block_presence.py
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

from genie import InMemoryBackend, Memory, MemoryNotFound

MISSING = "block-that-was-never-stored"


def main() -> None:
    """Run the membership tests and show the absence that each API reports."""

    print("== Block presence ==")
    memory = Memory(backend=InMemoryBackend(), principal="app:demo")
    project = memory.put(type="project", value="Launch notes")
    note = memory.put(parent=project, type="note", value="Draft the outline")
    checklist = memory.put(parent=note, type="checklist", value=["draft", "review"])
    unrelated = memory.put(type="project", value="Another workspace")

    # `contains` follows ownership at all depths. A root contains itself.
    print(f"project contains its note: {memory.contains(project, note)}")
    print(f"project contains a grandchild: {memory.contains(project, checklist)}")
    print(f"project contains an unrelated block: {memory.contains(project, unrelated)}")

    assert memory.contains(project, note)
    assert memory.contains(project, checklist)
    assert memory.contains(project, project)
    assert not memory.contains(project, unrelated)
    assert not memory.contains(note, project)

    # A missing candidate gives a reportable answer, not an error.
    assert not memory.contains(project, MISSING)
    assert memory.get(MISSING) is None
    print("reading a block that was never stored: None")

    # A missing root gives an error, because there is no subtree to test.
    try:
        memory.contains(MISSING, note)
    except MemoryNotFound:
        print("membership test against a missing root: MemoryNotFound")
    else:
        raise AssertionError("A membership test needs an existing root.")

    # An operation that acts on one block raises `MemoryNotFound`. It does not
    # skip the missing block silently.
    try:
        memory.update(MISSING, metadata={"reviewed": True})
    except MemoryNotFound:
        print("updating a block that was never stored: MemoryNotFound")
    else:
        raise AssertionError("Updating a missing block must fail.")
    try:
        memory.put(parent=MISSING, type="note", value="Orphan")
    except MemoryNotFound:
        print("writing under a missing parent: MemoryNotFound")
    else:
        raise AssertionError("Writing under a missing parent must fail.")
    try:
        memory.add_link(project, MISSING, "informs")
    except MemoryNotFound:
        print("linking to a block that was never stored: MemoryNotFound")
    else:
        raise AssertionError("Linking to a missing block must fail.")

    # A delete of the note also deletes the checklist that it owned, recursively.
    # After that, membership tests for both blocks return `False` and do not raise.
    memory.delete(note)

    assert not memory.contains(project, note)
    assert not memory.contains(project, checklist)
    assert memory.get(note) is None
    print("after deletion the note is absent, not an error")


if __name__ == "__main__":
    main()
