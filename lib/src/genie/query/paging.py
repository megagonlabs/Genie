"""Portable search order and cursor paging for backend adapters."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


def page_results(results: Iterable[SearchResult], query: MemoryQuery) -> list[SearchResult]:
    """Sort search results in the search order, and keep one page.

    - **Search order:** `score` from high to low, then `created_at` from old
      to new, then block id in ascending order. So results with the same
      score come in creation order. Each block id occurs one time, so 2
      results never tie.
    - **Position:** each result sorts by its `cursor`. So a result with a
      `position` sorts by it, and the sort agrees with the cursor that the
      caller gets. An application can merge the pages of 2 `Memory` searches
      with this helper.
    - **Cursor:** drops each result at or before `query.after`.
    - **Limit:** keeps the first `query.limit` of the remaining results.
    - **Shared:** each shipped adapter uses this helper after it scores its
      candidates. A custom adapter that ranks in an external engine returns
      the same order and page in a different way.

    Args:
        results: Scored results in any order.
        query: Query that gives `after` and `limit`.

    Returns:
        At most `query.limit` results after `query.after`, in search order.
    """

    ordered = sorted(results, key=_order_key)
    if query.after is not None:
        start = (-query.after.score, query.after.created_at, query.after.block_id)
        ordered = [result for result in ordered if _order_key(result) > start]
    return ordered[: query.limit]


def _order_key(result: SearchResult) -> tuple[float, datetime, str]:
    """Return the sort key of a result in the search order, from its `cursor`."""

    cursor = result.cursor
    return (-cursor.score, cursor.created_at, cursor.block_id)
