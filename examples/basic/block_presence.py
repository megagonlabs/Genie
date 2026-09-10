"""Test subtree membership and tell reportable absence from a real error.

Genie separates two kinds of absence. A read of a block that is not there is an
answer: `get` returns `None` and `contains` returns `False`. An operation that
must act on a specific block cannot report that as a value, so it raises
`MemoryNotFound`.

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
    """Walk membership tests and the absence each API reports."""

    print("== Block presence ==")
    memory = Memory(backend=InMemoryBackend(), principal="app:demo")
    project = memory.put(type="project", value="Launch notes")
    note = memory.put(parent=project, type="note", value="Draft the outline")
    checklist = memory.put(parent=note, type="checklist", value=["draft", "review"])
    unrelated = memory.put(type="project", value="Another workspace")

    # contains follows ownership at any depth, and a root contains itself.
    print(f"project contains its note: {memory.contains(project, note)}")
    print(f"project contains a grandchild: {memory.contains(project, checklist)}")
    print(f"project contains an unrelated block: {memory.contains(project, unrelated)}")

    assert memory.contains(project, note)
    assert memory.contains(project, checklist)
    assert memory.contains(project, project)
    assert not memory.contains(project, unrelated)
    assert not memory.contains(note, project)

    # A missing candidate is a reportable answer rather than an error.
    assert not memory.contains(project, MISSING)
    assert memory.get(MISSING) is None
    print("reading a block that was never stored: None")

    # A missing root is not, because there is no subtree to test against.
    try:
        memory.contains(MISSING, note)
    except MemoryNotFound:
        print("membership test against a missing root: MemoryNotFound")
    else:
        raise AssertionError("A membership test needs an existing root.")

    # Operations that must act on one block raise instead of doing nothing.
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

    # Deleting the note recursively removes the checklist it owned, and both
    # then answer membership tests with False rather than raising.
    memory.delete(note)

    assert not memory.contains(project, note)
    assert not memory.contains(project, checklist)
    assert memory.get(note) is None
    print("after deletion the note is absent, not an error")


if __name__ == "__main__":
    main()
