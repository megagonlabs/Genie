"""Keep a link relation acyclic with atomic inserts that check for cycles.

- **`add_link`:** stores any typed link.
- **`protect_relation`:** trusted code protects a relation once. After that,
  a plain `add_link` with the relation fails for all callers.
- **`add_acyclic_link`:** only for a protected relation. The backend checks
  reachability and inserts the link in one operation. So a relation for a
  derivation graph or a dependency graph cannot get a cycle, also when
  concurrent writers add links.
- **One relation only:** the cycle check covers only links with the exact same
  relation. So one pair of blocks can be acyclic in one relation and cyclic in
  another relation.

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

    # "derives" is a derivation DAG (a directed graph with no cycles). An
    # acyclic insert needs a protected relation.
    try:
        memory.add_acyclic_link(raw, cleaned, "derives")
    except MemoryConflict:
        print("acyclic insert before protect_relation: refused")
    else:
        raise AssertionError("An acyclic insert must need a protected relation.")

    # Trusted code protects the relation once. Then `add_acyclic_link` checks
    # each insert for a cycle.
    memory.protect_relation("derives")
    memory.add_acyclic_link(raw, cleaned, "derives")
    memory.add_acyclic_link(cleaned, trained, "derives")

    try:
        memory.add_acyclic_link(trained, raw, "derives")
    except MemoryConflict:
        print("closing the derivation loop: refused")
    else:
        raise AssertionError("An acyclic relation must reject a cycle.")

    # `Memory` does not store the refused link, so the DAG does not change.
    assert [link.target_id for link in memory.list_links(trained, relation="derives")] == []
    assert [link.target_id for link in memory.list_links(raw, relation="derives")] == [cleaned.id]

    # A block also cannot derive from itself.
    try:
        memory.add_acyclic_link(raw, raw, "derives")
    except MemoryConflict:
        print("a block deriving from itself: refused")
    else:
        raise AssertionError("An acyclic relation must reject a self link.")

    # A plain link cannot go around the cycle check of a protected relation.
    try:
        memory.add_link(trained, raw, "derives")
    except MemoryConflict:
        print("plain link in the protected relation: refused")
    else:
        raise AssertionError("A protected relation must reject a plain link.")

    # "mentions" is an ordinary relation. `add_link` does no reachability check,
    # so the same 3 blocks can form a cycle in a different relation.
    memory.add_link(raw, cleaned, "mentions")
    memory.add_link(cleaned, trained, "mentions")
    closing = memory.add_link(trained, raw, "mentions")

    print(f"cycle allowed in an ordinary relation: {closing.relation}")

    assert [link.target_id for link in memory.list_links(trained, relation="mentions")] == [raw.id]

    # Mounts are symbolic pointers, not a DAG. So `Memory` rejects the reserved
    # mount relation with `ValueError` before it sends an insert to the backend.
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
