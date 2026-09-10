"""Rewrite a search request and review its results with transforming hooks.

`before_search` receives the normalized `MemoryQuery` and returns the query that
actually runs, so a guardrail can narrow what a caller asked for.
`after_search` receives the results and returns what the caller receives, so a
redaction step can remove blocks before they leave the memory system.

Both hooks fail fast and ignore `fail_open`: a contained failure would hand back
the unreviewed query or results the hook existed to change.

Run from the repository root:

    python examples/basic/search_hooks.py
"""

from __future__ import annotations

from dataclasses import replace

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    HookContext,
    InMemoryBackend,
    Memory,
    MemoryHook,
    MemoryQuery,
    SearchResult,
)


class QueryGuard(MemoryHook):
    """Cap how much memory one search may pull back.

    Attributes:
        max_limit: Largest limit this guard allows.
        clamped: Limits that were requested and then reduced.
        seen_filters: Filters as the hook received them, already normalized.
    """

    def __init__(self, *, max_limit: int) -> None:
        """Create a guard for one maximum limit.

        Args:
            max_limit: Largest limit to allow through.
        """

        self.max_limit = max_limit
        self.clamped: list[int] = []
        self.seen_filters: list[dict[str, object]] = []

    def before_search(self, context: HookContext, query: MemoryQuery) -> MemoryQuery:
        """Return the query that should actually run."""

        self.seen_filters.append(dict(query.filters))
        if query.limit <= self.max_limit:
            return query
        self.clamped.append(query.limit)
        return replace(query, limit=self.max_limit)


class Redactor(MemoryHook):
    """Remove confidential blocks from what a caller receives.

    Attributes:
        offline: Whether the redaction service is unreachable.
        removed: Number of results withheld so far.
    """

    # Declared, but `after_search` ignores it: returning unredacted results
    # would defeat the hook.
    fail_open = True

    def __init__(self) -> None:
        """Create a reachable redactor."""

        self.offline = False
        self.removed = 0

    def after_search(
        self,
        context: HookContext,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Return only the results the caller may see.

        Raises:
            RuntimeError: If the redaction service is unreachable.
        """

        if self.offline:
            raise RuntimeError("redaction service unreachable")
        kept = [result for result in results if not result.block.metadata.get("confidential")]
        self.removed += len(results) - len(kept)
        return kept


def main() -> None:
    """Narrow a query and review its results before the caller sees them."""

    print("== Search hooks ==")
    guard = QueryGuard(max_limit=2)
    redactor = Redactor()
    memory = Memory(
        backend=InMemoryBackend(),
        principal="app:search",
        hooks=[guard, redactor],
    )

    project = memory.put(type="project", value="Retrieval review")
    for index in range(4):
        memory.put(
            parent=project,
            type="finding",
            value=f"Retrieval finding {index}",
            metadata={"priority": index},
        )
    secret = memory.put(
        parent=project,
        type="finding",
        value="Retrieval finding from an embargoed partner",
        metadata={"priority": 9, "confidential": True},
    )

    results = memory.search(
        "retrieval",
        root=project,
        scope="subtree",
        filters={"metadata.priority": {">=": 0}},
        limit=50,
    )

    print(f"requested limit: 50, limits clamped by the guard: {guard.clamped}")
    print(f"filters as the hook received them: {guard.seen_filters[0]}")
    print(f"results returned to the caller: {len(results)}")

    # The guard capped the search before it ran.
    assert guard.clamped == [50]
    assert len(results) <= guard.max_limit
    # Friendly operators are normalized before any hook sees the query.
    assert guard.seen_filters[0] == {"metadata.priority": {"$gte": 0}}

    # Nothing confidential reached the caller.
    assert all(not result.block.metadata.get("confidential") for result in results)

    # Raise the cap so the confidential block is a candidate, and confirm the
    # redactor is what keeps it out.
    wide = memory.search("embargoed", root=project, scope="subtree", limit=2)

    print(f"results mentioning the embargoed finding: {len(wide)}")
    print(f"results withheld by the redactor: {redactor.removed}")

    assert wide == []
    assert redactor.removed >= 1
    # The block itself is readable; only search results are reviewed.
    assert memory.get(secret) is not None

    # A failing result review stops the search instead of returning results
    # nobody reviewed, even though the hook declares `fail_open`.
    redactor.offline = True
    try:
        memory.search("retrieval", root=project, scope="subtree")
    except RuntimeError:
        print("search while the redactor is offline: refused, not unredacted")
    else:
        raise AssertionError("A failing after_search hook must not return results.")

    # Reads that do not produce search results are unaffected.
    assert memory.get(secret) is not None
    redactor.offline = False


if __name__ == "__main__":
    main()
