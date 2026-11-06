"""Rewrite a search request and review its results with hooks that change them.

- **`before_search`:** gets the normalized `MemoryQuery` and returns the query
  that runs. So a guardrail can narrow the request of a caller.
- **`after_search`:** gets the results and returns the results that the caller
  gets. So a redaction step can remove blocks before they leave the memory
  system.
- **Fail fast:** both hooks fail fast and ignore `fail_open`. The hook exists to
  change the query or the results. So a failure stops the search. The search
  does not run the unreviewed query or return the unreviewed results.

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
    """Cap the `limit` of each search query.

    Attributes:
        max_limit: The largest `limit` that this guard allows.
        clamped: The requested limits that the guard reduced.
        seen_filters: The filters as the hook got them, in normalized form.
    """

    def __init__(self, *, max_limit: int) -> None:
        """Create a guard for one maximum limit.

        Args:
            max_limit: The largest `limit` that the guard allows.
        """

        self.max_limit = max_limit
        self.clamped: list[int] = []
        self.seen_filters: list[dict[str, object]] = []

    def before_search(self, context: HookContext, query: MemoryQuery) -> MemoryQuery:
        """Return the query that runs, with its `limit` reduced to `max_limit` if it is larger."""

        self.seen_filters.append(dict(query.filters))
        if query.limit <= self.max_limit:
            return query
        self.clamped.append(query.limit)
        return replace(query, limit=self.max_limit)


class Redactor(MemoryHook):
    """Remove confidential blocks from the results that a caller gets.

    Attributes:
        offline: `True` if the redaction service is unreachable.
        removed: The number of results that the redactor removed until now.
    """

    # The hook declares `fail_open`, but the hook registry ignores it for
    # `after_search`. So a failure of the hook never returns unredacted results.
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
        """Return only the results that the caller can see.

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
    # `MemoryQuery` normalizes the friendly operators before a hook sees the query.
    assert guard.seen_filters[0] == {"metadata.priority": {"$gte": 0}}

    # Nothing confidential reached the caller.
    assert all(not result.block.metadata.get("confidential") for result in results)

    # Search for a word that only the confidential block has, so the block is a
    # candidate. Then confirm that the redactor is the step that removes it.
    wide = memory.search("embargoed", root=project, scope="subtree", limit=2)

    print(f"results mentioning the embargoed finding: {len(wide)}")
    print(f"results withheld by the redactor: {redactor.removed}")

    assert list(wide) == []
    assert redactor.removed >= 1
    # The block itself stays readable. The redactor reviews only search results.
    assert memory.get(secret) is not None

    # If the result review fails, the search raises the error. It does not return
    # results that nobody reviewed, although the hook declares `fail_open`.
    redactor.offline = True
    try:
        memory.search("retrieval", root=project, scope="subtree")
    except RuntimeError:
        print("search while the redactor is offline: refused, not unredacted")
    else:
        raise AssertionError("A failing after_search hook must not return results.")

    # The offline redactor has no effect on reads that do not return search results.
    assert memory.get(secret) is not None
    redactor.offline = False


if __name__ == "__main__":
    main()
