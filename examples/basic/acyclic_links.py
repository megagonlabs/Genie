"""Keep a link relation acyclic with atomic cycle-checked inserts.

`add_link` stores any typed link. `add_acyclic_link` asks the backend to check
reachability and insert as one operation, so a relation used as a derivation or
dependency graph cannot grow a cycle even under concurrent writers.

Cycle detection covers only links with the exact same relation, so one pair of
blocks can be acyclic in one relation and cyclic in another.

Run from the repository root:

    python examples/basic/acyclic_links.py
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

from genie import (
    InMemoryBackend,
    LinkRelations,
    Memory,
    MemoryConflict,
)


def main() -> None:
    """Build one acyclic relation next to one ordinary relation."""

    print("== Acyclic links ==")
    memory = Memory(backend=InMemoryBackend(), principal="app:pipeline")
    raw = memory.put(type="dataset", value="raw crawl")
    cleaned = memory.put(type="dataset", value="deduplicated crawl")
    trained = memory.put(type="model", value="ranker trained on the clean set")

    # "derives" is a derivation DAG, so every insert is cycle checked.
    memory.add_acyclic_link(raw, cleaned, "derives")
    memory.add_acyclic_link(cleaned, trained, "derives")

    try:
        memory.add_acyclic_link(trained, raw, "derives")
    except MemoryConflict:
        print("closing the derivation loop: refused")
    else:
        raise AssertionError("An acyclic relation must reject a cycle.")

    # The refused insert is not stored, so the DAG is unchanged.
    assert [link.target_id for link in memory.list_links(trained, relation="derives")] == []
    assert [link.target_id for link in memory.list_links(raw, relation="derives")] == [cleaned.id]

    # A block cannot derive from itself either.
    try:
        memory.add_acyclic_link(raw, raw, "derives")
    except MemoryConflict:
        print("a block deriving from itself: refused")
    else:
        raise AssertionError("An acyclic relation must reject a self link.")

    # "mentions" is an ordinary relation. `add_link` performs no reachability
    # check, so the same three blocks may form a cycle in a different relation.
    memory.add_link(raw, cleaned, "mentions")
    memory.add_link(cleaned, trained, "mentions")
    closing = memory.add_link(trained, raw, "mentions")

    print(f"cycle allowed in an ordinary relation: {closing.relation}")

    assert [link.target_id for link in memory.list_links(trained, relation="mentions")] == [raw.id]

    # Mounts are symbolic pointers rather than a DAG, so the reserved relation
    # is rejected before the backend is asked to insert anything.
    try:
        memory.add_acyclic_link(raw, trained, LinkRelations.MOUNT)
    except ValueError:
        print("acyclic insert of the reserved mount relation: refused")
    else:
        raise AssertionError("The reserved mount relation must not be inserted acyclically.")

    derivations = memory.list_links(cleaned, relation="derives", direction="both")
    print(f"derivation edges touching the clean set: {len(derivations)}")

    assert {link.source_id for link in derivations} == {raw.id, cleaned.id}


if __name__ == "__main__":
    main()
