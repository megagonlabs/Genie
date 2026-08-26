"""Search memory with friendly arguments and portable query objects.

Run from the repository root:

    conda run -n "genie" python examples/basic/search_filters.py
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

from genie import InMemoryBackend, Memory, MemoryQuery, Scope, SearchResult


def main() -> None:
    """Run a focused search and filter walkthrough."""

    print("== Search and filters ==")
    memory = Memory(backend=InMemoryBackend())
    project = memory.put(type="project", value="Proposal workspace")
    selected = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database agnostic.",
        metadata={"priority": 3, "owner": "team"},
    )
    memory.put(
        parent=project,
        type="requirement",
        value="Include optional implementation notes.",
        metadata={"priority": 1, "owner": "team"},
    )
    memory.put(type="note", value="Global note outside the project", metadata={"priority": 5})

    # Memory.search accepts friendly keyword arguments for application code.
    friendly_results = memory.search(
        "proposal",
        root=project,
        scope="subtree",
        types=["requirement"],
        filters={"metadata.priority": {">=": 2}, "owner": {"in": ["team"]}},
    )

    # `MemoryQuery` is the validated, backend-portable request shape. Friendly
    # filter operators are normalized when the query is constructed.
    query = MemoryQuery(
        text="proposal",
        root_id=project.id,
        scope=Scope.SUBTREE,
        types=["requirement"],
        filters={"metadata.priority": {">=": 2}, "owner": {"in": ["team"]}},
        limit=10,
    )
    results: list[SearchResult] = memory.search(query)

    print("query: 'proposal' in the project subtree, priority >= 2")
    print(f"normalized filters: {query.filters}")
    print(f"matches: {len(results)}")
    for result in results:
        print(
            f"- {result.block.type}: {result.block.value} "
            f"(score={result.score}, reason={result.reason!r})"
        )

    assert results == friendly_results
    assert query.scope is Scope.SUBTREE
    assert query.filters == {
        "metadata.priority": {"$gte": 2},
        "owner": {"$in": ["team"]},
    }
    assert [result.block.id for result in results] == [selected.id]


if __name__ == "__main__":
    main()
