"""Tests for the search order, the search cursor, and the visible-result limit of `Memory.search`."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    HookContext,
    InMemoryBackend,
    Memory,
    MemoryBackend,
    MemoryBlock,
    MemoryHook,
    MemoryPolicy,
    MemoryQuery,
    RoutedBackend,
    Scope,
    SearchCursor,
    SearchResult,
    SQLiteBackend,
    page_results,
)

_FIRST = datetime(2026, 1, 1, tzinfo=UTC)
_SECOND = datetime(2026, 1, 2, tzinfo=UTC)


def _ids(results: list[SearchResult]) -> list[str]:
    """Return the block ids of search results, in result order."""

    return [result.block.id for result in results]


class _RecordingBackend(InMemoryBackend):
    """In-memory backend that records each search query it gets."""

    def __init__(self) -> None:
        """Start with an empty query log."""

        super().__init__()
        self.queries: list[MemoryQuery] = []

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Record the query, then search normally."""

        self.queries.append(query)
        return super().search(query)


def test_search_cursor_and_query_check_their_fields() -> None:
    """`SearchCursor` and `MemoryQuery.after` raise an error for a malformed field."""

    cursor = SearchCursor(score=2, created_at=_FIRST, block_id="mem_a")
    block = MemoryBlock(id="mem_a", type="note", value="a", created_at=_FIRST)

    assert cursor.score == 2.0
    assert SearchResult(block, score=2).cursor == cursor
    assert MemoryQuery(after=cursor).after == cursor
    with pytest.raises(TypeError, match="score must be a number"):
        SearchCursor(score=cast(float, True), created_at=_FIRST, block_id="mem_a")
    with pytest.raises(ValueError, match="score must be finite"):
        SearchCursor(score=math.inf, created_at=_FIRST, block_id="mem_a")
    with pytest.raises(TypeError, match="created_at must be a datetime"):
        SearchCursor(score=1, created_at=cast(datetime, "2026-01-01"), block_id="mem_a")
    with pytest.raises(ValueError, match="created_at must be timezone-aware"):
        SearchCursor(score=1, created_at=datetime(2026, 1, 1), block_id="mem_a")  # noqa: DTZ001 - invalid input
    with pytest.raises(ValueError, match="block_id must not be empty"):
        SearchCursor(score=1, created_at=_FIRST, block_id=" ")
    with pytest.raises(TypeError, match="after must be a SearchCursor"):
        MemoryQuery(after=cast(Any, "mem_a"))


def test_page_results_sorts_by_score_then_creation_time_then_block_id() -> None:
    """`page_results` puts higher scores first, then older blocks, then lower block ids, and pages after a cursor."""

    def result(block_id: str, score: float, created_at: datetime) -> SearchResult:
        return SearchResult(MemoryBlock(id=block_id, type="note", value=block_id, created_at=created_at), score=score)

    results = [
        result("mem_c", 1, _SECOND),
        result("mem_b", 1, _FIRST),
        result("mem_d", 1, _SECOND),
        result("mem_a", 2, _SECOND),
    ]

    assert _ids(page_results(results, MemoryQuery())) == ["mem_a", "mem_b", "mem_c", "mem_d"]
    assert _ids(page_results(results, MemoryQuery(limit=2))) == ["mem_a", "mem_b"]
    assert _ids(page_results(results, MemoryQuery(after=results[1].cursor))) == ["mem_c", "mem_d"]
    # A cursor is only a position. It does not have to name one of the results.
    between = SearchCursor(score=1.5, created_at=_FIRST, block_id="mem_missing")
    assert _ids(page_results(results, MemoryQuery(after=between))) == ["mem_b", "mem_c", "mem_d"]
    assert page_results(results, MemoryQuery(limit=0)) == []


@pytest.fixture(params=["in_memory", "sqlite"])
def make_backend(request: pytest.FixtureRequest) -> Iterator[Callable[[], MemoryBackend]]:
    """Give a factory for new backends of one kind, and close them after the test."""

    opened: list[SQLiteBackend] = []

    def make() -> MemoryBackend:
        if request.param == "in_memory":
            return InMemoryBackend()
        backend = SQLiteBackend()
        opened.append(backend)
        return backend

    yield make
    for backend in opened:
        backend.close()


@pytest.mark.parametrize(
    "search_options",
    [
        pytest.param({"query": "quarterly report"}, id="text"),
        pytest.param({"filters": {"value": {"$in": ["secret plan", "quarterly report draft"]}}}, id="value-filter"),
        pytest.param({"filters": {"metadata.salary": {"$gte": 100_000}}}, id="metadata-filter"),
        pytest.param({"filters": {"created_by": {"$in": ["agent:a", "agent:b"]}}}, id="created-by-filter"),
    ],
)
def test_hidden_blocks_do_not_change_visible_search_results(make_backend: Callable[[], MemoryBackend], search_options: dict[str, Any]) -> None:
    """A search gives the same visible results with or without matching blocks that the caller cannot see.

    - **The leak this prevents:** when the backend cut the candidates to
      `limit` before the policy filter, a hidden block that ranked first took
      the only result slot. An empty page then told the caller that a hidden
      block matched the text or the filter. With a range filter, the caller
      could find a hidden number with a binary search.
    - **Setup:** the hidden blocks are older than the visible note, and one of
      them has a higher text score. So without the fix, each of them sorts
      before the visible note.
    """

    def visible_values(*, with_hidden: bool) -> list[Any]:
        backend = make_backend()
        task = Memory(backend=backend, principal="svc").put(type="task", value="shared task", policy=MemoryPolicy.public())
        if with_hidden:
            # `owner_private` is the default policy, so agent:a cannot see these.
            other = Memory(backend=backend, principal="agent:b")
            other.put(parent=task, type="note", value="quarterly report quarterly report", metadata={"salary": 137_000})
            other.put(parent=task, type="note", value="secret plan", metadata={"salary": 137_000})
        agent = Memory(backend=backend, principal="agent:a").get_handle(root=task)
        agent.put(type="note", value="quarterly report draft", metadata={"salary": 10**12})
        return [result.block.value for result in agent.search(types=["note"], limit=1, **search_options)]

    assert visible_values(with_hidden=False) == ["quarterly report draft"]
    assert visible_values(with_hidden=True) == ["quarterly report draft"]


def test_search_limit_counts_visible_results_and_doubles_each_batch() -> None:
    """Hidden matches do not use part of the limit. `Memory` asks for the next page with 2 times the batch size."""

    backend = _RecordingBackend()
    owner = Memory(backend=backend, principal="owner")
    other = Memory(backend=backend, principal="other")
    for _ in range(10):
        other.put(type="note", value="zz zz zz zz zz")
    third = owner.put(type="note", value="zz")
    first = owner.put(type="note", value="zz zz zz")
    second = owner.put(type="note", value="zz zz")
    backend.queries.clear()

    results = owner.search("zz", limit=2)

    assert _ids(results) == [first.id, second.id]
    assert [query.limit for query in backend.queries] == [2, 4, 8]
    assert backend.queries[0].after is None
    assert all(query.after is not None for query in backend.queries[1:])
    assert _ids(owner.search("zz", limit=5)) == [first.id, second.id, third.id]

    backend.queries.clear()
    assert owner.search("zz", limit=0) == []
    assert backend.queries == []


def test_search_after_returns_the_next_visible_page() -> None:
    """A `MemoryQuery` with `after` set to the cursor of the last result continues the same order with no gap or repeat."""

    memory = Memory(principal="owner")
    other = Memory(backend=memory.backend, principal="other")
    for score in range(1, 6):
        memory.put(type="note", value=" ".join(["zz"] * score))
        other.put(type="note", value=" ".join(["zz"] * score))

    pages: list[list[str]] = []
    after: SearchCursor | None = None
    while True:
        page = memory.search(MemoryQuery("zz", limit=2, after=after))
        if not page:
            break
        pages.append(_ids(page))
        after = page[-1].cursor

    assert [len(page) for page in pages] == [2, 2, 1]
    assert [block_id for page in pages for block_id in page] == _ids(memory.search("zz", limit=10))


def test_search_after_keyword_matches_the_query_field() -> None:
    """The `after` keyword of `Memory.search` gives the same page as `MemoryQuery.after`, and it checks its type."""

    memory = Memory(principal="owner")
    for score in range(1, 6):
        memory.put(type="note", value=" ".join(["zz"] * score))
    first = memory.search("zz", limit=2)

    second = memory.search("zz", limit=2, after=first[-1].cursor)

    assert _ids(second) == _ids(memory.search(MemoryQuery("zz", limit=2, after=first[-1].cursor)))
    assert _ids(first + second) == _ids(memory.search("zz", limit=4))
    with pytest.raises(TypeError, match="after must be a SearchCursor"):
        memory.search("zz", after=cast(Any, "mem_a"))


def test_handle_search_pages_in_the_default_scope() -> None:
    """A rooted handle pages in its `default_scope`, with the `after` keyword or with a `MemoryQuery` that has no scope."""

    memory = Memory(principal="owner")
    root = memory.put(type="root", value="workspace")
    memory.put(type="note", value="zz zz zz")
    for score in range(1, 6):
        memory.put(parent=root, type="note", value=" ".join(["zz"] * score))
    handle = memory.get_handle(root=root)

    pages: list[list[str]] = []
    after: SearchCursor | None = None
    while page := handle.search("zz", limit=2, after=after):
        pages.append(_ids(page))
        after = page[-1].cursor

    assert [len(page) for page in pages] == [2, 2, 1]
    assert [block_id for page in pages for block_id in page] == _ids(memory.search("zz", root=root, scope="subtree", limit=10))
    first = handle.search("zz", limit=2)
    assert _ids(handle.search(MemoryQuery("zz", limit=2, after=first[-1].cursor))) == pages[1]
    assert _ids(handle.search(MemoryQuery("zz", limit=2, after=first[-1].cursor, scope="subtree"))) == pages[1]
    with pytest.raises(AccessDenied, match="global search"):
        handle.search(MemoryQuery("zz", limit=2, after=first[-1].cursor, scope="global"))


def test_search_pages_a_memory_query_with_the_after_keyword() -> None:
    """`search(query, after=cursor)` returns the next page of `query` on `Memory` and on a handle, so a paging loop ends."""

    memory = Memory(principal="owner")
    root = memory.put(type="root", value="workspace")
    memory.put(type="note", value="zz zz zz")
    for score in range(1, 6):
        memory.put(parent=root, type="note", value=" ".join(["zz"] * score))
    query = MemoryQuery("zz", root_id=root.id, scope="subtree", limit=2)
    expected = _ids(memory.search(replace(query, limit=10)))

    for search in (memory.search, memory.get_handle(root=root).search):
        pages: list[list[str]] = []
        after: SearchCursor | None = None
        # The page count check stops the loop if `after` is lost.
        while (page := search(query, after=after)) and len(pages) < 5:
            pages.append(_ids(page))
            after = page[-1].cursor

        assert [len(page) for page in pages] == [2, 2, 1]
        assert [block_id for page in pages for block_id in page] == expected


def test_backend_gets_a_scope_for_a_query_with_no_scope() -> None:
    """`Memory` and a handle give the backend a `Scope`, also for a `MemoryQuery` with no scope."""

    backend = _RecordingBackend()
    memory = Memory(backend=backend, principal="owner")
    root = memory.put(type="root", value="zz")
    memory.put(parent=root, type="note", value="zz")

    memory.search(MemoryQuery("zz"))
    memory.get_handle(root=root).search(MemoryQuery("zz"))

    assert MemoryQuery("zz").scope is None
    assert [query.scope for query in backend.queries] == [Scope.GLOBAL, Scope.SUBTREE]


def test_search_counts_a_block_once_when_it_moves_past_the_cursor() -> None:
    """A block that changes between 2 backend pages can come back on the second page. The search keeps only its first result."""

    class ChangingBackend(InMemoryBackend):
        """Backend that runs a change just before it answers the second page."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.change: Callable[[], object] | None = None

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            self.calls += 1
            if self.calls == 2 and self.change is not None:
                self.change()
            return super().search(query)

    backend = ChangingBackend()
    owner = Memory(backend=backend, principal="owner")
    other = Memory(backend=backend, principal="other")
    # The text search also reads metadata. So a metadata update changes the score.
    moved = owner.put(type="note", value="moved", metadata={"tag": "zz zz zz"})
    other.put(type="note", value="zz zz zz")
    other.put(type="note", value="zz zz")
    last = owner.put(type="note", value="zz")
    # A second `Memory` on the same backend, so the update runs as its own
    # operation while the search is open.
    writer = Memory(backend=backend, principal="owner")
    backend.change = lambda: writer.update(moved, metadata={"tag": "zz"})
    backend.calls = 0

    results = owner.search("zz", limit=2)

    assert backend.calls == 2
    assert _ids(results) == [moved.id, last.id]


def test_search_ends_when_a_backend_ignores_the_cursor() -> None:
    """A backend that always starts from the first result still ends, because each batch is larger than the one before."""

    class CursorIgnoringBackend(InMemoryBackend):
        """Backend that drops `after` from each query."""

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            return super().search(replace(query, after=None))

    backend = CursorIgnoringBackend()
    owner = Memory(backend=backend, principal="owner")
    other = Memory(backend=backend, principal="other")
    for _ in range(5):
        other.put(type="note", value="zz zz")
    first = owner.put(type="note", value="zz")
    second = owner.put(type="note", value="zz")
    expected = sorted([first, second], key=lambda block: (block.created_at, block.id))

    assert _ids(owner.search("zz", limit=2)) == [block.id for block in expected]


def test_handle_search_limit_skips_results_outside_the_handle_root() -> None:
    """In a `links` search of a rooted handle, a linked block outside the root does not use part of the limit."""

    memory = Memory(principal="owner")
    root = memory.put(type="root", value="workspace")
    inside = memory.put(parent=root, type="note", value="zz")
    outside = memory.put(type="note", value="zz zz")
    memory.add_link(inside, outside, "cites")
    handle = memory.get_handle(root=root)

    assert _ids(memory.search("zz", root=inside, scope="links", limit=1)) == [outside.id]
    assert _ids(handle.search("zz", root=inside, scope="links", limit=1)) == [inside.id]


# Caller paging over a search index whose copy differs from the stored block.


class _CopyIndex:
    """Search index that sorts and pages its own copy of each block, as a separate search engine does.

    - **Index copy:** `index` stores a copy of a block. The copy can have a
      different `created_at` from the stored block.
    - **Order:** the search order uses the copies. So the `created_at` of a
      copy decides its position.
    - **Matches:** a copy matches when its value contains the query text.
    """

    def __init__(self) -> None:
        """Start with an empty index."""

        self.copies: dict[str, MemoryBlock] = {}

    def index(self, block: MemoryBlock, created_at: datetime) -> None:
        """Store a copy of a block with the `created_at` that the index keeps."""

        self.copies[block.id] = replace(block, created_at=created_at)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Return one page of the matching copies in the search order."""

        text = query.text or ""
        return page_results([SearchResult(copy) for copy in self.copies.values() if text in str(copy.value)], query)


class _IndexCopyBackend(InMemoryBackend):
    """In-memory backend whose search reads its `_CopyIndex`, not the stored blocks."""

    def __init__(self) -> None:
        """Start with an empty index."""

        super().__init__()
        self.copy_index = _CopyIndex()

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Return one page from the index copies."""

        return self.copy_index.search(query)


def _store_notes(backend: MemoryBackend, index: _CopyIndex, count: int, *, prefix: str = "mem") -> list[str]:
    """Store notes of `owner` 0.1 ms apart, index each one at millisecond precision, and return their ids in search order.

    - **Fixed times:** each note has a fixed `created_at`, so the test does not
      depend on the clock.
    - **Index copy:** the index keeps `created_at` at millisecond precision, as
      many search engines do. So 10 notes share each millisecond in the index.
    """

    ids: list[str] = []
    for number in range(count):
        created_at = _FIRST + timedelta(microseconds=100 * number)
        note = backend.put_block(MemoryBlock(id=f"{prefix}_{number:02}", type="note", value=f"zz {number}", created_at=created_at, created_by="owner"))
        index.index(note, created_at.replace(microsecond=created_at.microsecond // 1000 * 1000))
        ids.append(note.id)
    return ids


def _page_ids(search: Callable[..., list[SearchResult]], *, limit: int) -> list[str]:
    """Page through a search as a caller does, with `after=page[-1].cursor`, and return the block ids of all pages.

    Raises:
        AssertionError: If the paging does not end after 100 pages.
    """

    ids: list[str] = []
    after: SearchCursor | None = None
    for _ in range(100):
        page = search(limit=limit, after=after)
        if not page:
            return ids
        ids.extend(_ids(page))
        after = page[-1].cursor
    raise AssertionError("The paging did not end.")


class _EditEachResult(MemoryHook):
    """`after_search` hook that changes each result with one function."""

    def __init__(self, edit: Callable[[SearchResult], SearchResult]) -> None:
        """Keep the function that changes a result."""

        self.edit = edit

    def after_search(self, context: HookContext, query: MemoryQuery, results: list[SearchResult]) -> list[SearchResult]:
        """Return each result after the change."""

        return [self.edit(result) for result in results]


def _redacted(result: SearchResult) -> MemoryBlock:
    """Return the block of a result with its value masked."""

    return result.block.with_updates(value="[redacted]")


def test_search_result_position_sets_the_cursor() -> None:
    """`position` is keyword-only, `cursor` returns it, `replace` keeps it, and equality ignores it."""

    block = MemoryBlock(id="mem_a", type="note", value="a", created_at=_SECOND)
    position = SearchCursor(score=3, created_at=_FIRST, block_id="mem_a")
    result = SearchResult(block, score=2, position=position)

    assert result.cursor == position
    assert replace(result, block=replace(block, value="b")).cursor == position
    assert result == SearchResult(block, score=2)
    assert SearchResult(block, score=2).cursor == SearchCursor(score=2, created_at=_SECOND, block_id="mem_a")
    with pytest.raises(TypeError, match="position must be a SearchCursor"):
        SearchResult(block, position=cast(Any, "mem_a"))
    with pytest.raises(TypeError):
        cast(Any, SearchResult)(block, 2, None, position)


def test_caller_paging_follows_an_index_that_keeps_millisecond_created_at() -> None:
    """A caller who pages with `after=page[-1].cursor` gets each result when the index keeps `created_at` at millisecond precision.

    - **The bug this prevents:** `cursor` used the `created_at` of the stored
      block, but the index sorts by its own copy. So a cursor fell after the
      other results in the same millisecond, and the next page lost them.
    - **Content and position:** a result holds the stored block, and its
      cursor holds the time of the index copy.
    - **Equality:** the result equals a `SearchResult` of the block from
      `get`, because equality ignores `position`.
    """

    backend = _IndexCopyBackend()
    expected = _store_notes(backend, backend.copy_index, 20)
    memory = Memory(backend=backend, principal="owner")

    assert _page_ids(partial(memory.search, "zz"), limit=3) == expected
    second = memory.search("zz", limit=2)[1]
    assert second.block.created_at == _FIRST + timedelta(microseconds=100)
    assert second.cursor.created_at == _FIRST
    stored = memory.get(second.block.id)
    assert stored is not None
    assert second == SearchResult(stored, score=second.score, reason=second.reason)


def test_caller_paging_follows_the_shared_search_index_of_a_routed_backend() -> None:
    """Caller paging gets each result through `RoutedBackend` with a shared `search_index` that keeps `created_at` at millisecond precision.

    - **Setup of the bug report:** `RoutedBackend` sends each search to the
      shared index, and `Memory` reads the stored block from the route.
    """

    index = _CopyIndex()
    backend = RoutedBackend({"main": InMemoryBackend()}, search_index=index)
    expected = _store_notes(backend, index, 20)
    memory = Memory(backend=backend, principal="owner")

    assert _page_ids(partial(memory.search, "zz"), limit=3) == expected


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda result: replace(result, block=_redacted(result)), id="replace-block"),
        pytest.param(lambda result: SearchResult(_redacted(result), score=result.score, reason=result.reason), id="new-result"),
        pytest.param(lambda result: replace(result, score=result.score / 2), id="replace-score"),
    ],
)
def test_caller_paging_follows_the_index_after_an_after_search_hook(edit: Callable[[SearchResult], SearchResult]) -> None:
    """Caller paging gets each result when an `after_search` hook changes each result.

    - **`replace-block`:** `dataclasses.replace` keeps `position`.
    - **`new-result`:** the new result has no `position`. `Memory` gives back
      the position of the same block id.
    - **`replace-score`:** the cursor keeps the index score, not the score
      from the hook.
    """

    backend = _IndexCopyBackend()
    expected = _store_notes(backend, backend.copy_index, 20)
    memory = Memory(backend=backend, principal="owner", hooks=[_EditEachResult(edit)])

    assert _page_ids(partial(memory.search, "zz"), limit=3) == expected


def test_caller_paging_keeps_the_backend_score_after_a_hook_changes_it(make_backend: Callable[[], MemoryBackend]) -> None:
    """On a shipped backend, caller paging gets each result when an `after_search` hook changes the score.

    - **The bug this prevents:** the cursor used the score from the hook. The
      backend has no result after that position, so the second page was
      empty. This bug was older than the stored block in search results.
    """

    backend = make_backend()
    for _ in range(7):
        Memory(backend=backend, principal="owner").put(type="note", value="zz")
    expected = _ids(Memory(backend=backend, principal="owner").search("zz", limit=10))
    memory = Memory(backend=backend, principal="owner", hooks=[_EditEachResult(lambda result: replace(result, score=result.score / 2))])

    assert len(expected) == 7
    assert _page_ids(partial(memory.search, "zz"), limit=3) == expected


def test_after_search_position_is_filled_only_when_it_is_missing() -> None:
    """After `after_search`, `Memory` fills a missing position by block id, and it changes nothing else.

    - **Missing:** a new result for a block from the backend gets the index
      position of that block.
    - **Set by the hook:** the position stays.
    - **Not from the backend:** a result that the hook adds has no index
      position. So its cursor uses its block.
    """

    backend = _IndexCopyBackend()
    # The second note has a stored `created_at` that differs from its index
    # copy. So only the index position gives its correct cursor.
    _, second_id = _store_notes(backend, backend.copy_index, 2)
    extra = backend.put_block(MemoryBlock(id="mem_extra", type="note", value="not indexed", created_at=_SECOND, created_by="owner"))
    chosen = SearchCursor(score=9, created_at=_SECOND, block_id="mem_chosen")

    class Rebuild(MemoryHook):
        """Hook that builds new results: one with no position, one with its own position, and one for a block that the index did not return."""

        def after_search(self, context: HookContext, query: MemoryQuery, results: list[SearchResult]) -> list[SearchResult]:
            """Return the new results."""

            return [
                SearchResult(results[0].block, score=results[0].score, position=chosen),
                SearchResult(_redacted(results[1]), score=results[1].score),
                SearchResult(extra),
            ]

    results = Memory(backend=backend, principal="owner", hooks=[Rebuild()]).search("zz")

    assert [result.cursor for result in results] == [
        chosen,
        SearchCursor(score=1, created_at=_FIRST, block_id=second_id),
        SearchCursor(score=1, created_at=_SECOND, block_id="mem_extra"),
    ]
    assert results[1].block.value == "[redacted]"


def test_page_results_merges_the_pages_of_2_memory_searches() -> None:
    """An application that merges the pages of 2 `Memory` searches with `page_results` gets each result one time.

    - **The bug this prevents:** `page_results` sorted by the stored block, but
      the cursor came from the index copy. So the merged pages repeated and
      lost results.
    """

    left_backend, right_backend = _IndexCopyBackend(), _IndexCopyBackend()
    all_ids = _store_notes(left_backend, left_backend.copy_index, 20, prefix="left") + _store_notes(right_backend, right_backend.copy_index, 20, prefix="right")
    left = Memory(backend=left_backend, principal="owner")
    right = Memory(backend=right_backend, principal="owner")

    def merged(*, limit: int, after: SearchCursor | None) -> list[SearchResult]:
        pages = left.search("zz", limit=limit, after=after) + right.search("zz", limit=limit, after=after)
        return page_results(pages, MemoryQuery(limit=limit, after=after))

    paged = _page_ids(merged, limit=3)

    assert sorted(paged) == sorted(all_ids)
    assert paged == _ids(merged(limit=100, after=None))


def test_caller_paging_follows_an_index_that_has_an_old_copy_of_a_block_id() -> None:
    """Caller paging gets each result when the index has an old copy of a block id that was deleted and created again.

    - **The bug this prevents:** the new block has a later `created_at` than
      the index copy. So the cursor of the first result fell after all other
      results, and the second page was empty.
    - **Content:** the result holds the new stored block, not the old copy.
    - **Position:** the cursor holds the `created_at` of the old index copy,
      not of the new stored block.
    """

    backend = _IndexCopyBackend()
    for number, block_id in enumerate(["a", "b", "c", "d"]):
        block = backend.put_block(MemoryBlock(id=block_id, type="note", value=f"zz {block_id}", created_at=_FIRST + timedelta(seconds=number), created_by="owner"))
        backend.copy_index.index(block, block.created_at)
    memory = Memory(backend=backend, principal="owner")
    memory.delete("a")
    backend.put_block(MemoryBlock(id="a", type="note", value="zz a again", created_at=_SECOND, created_by="owner"))

    assert _page_ids(partial(memory.search, "zz"), limit=1) == ["a", "b", "c", "d"]
    first = memory.search("zz", limit=1)[0]
    assert first.block.value == "zz a again"
    assert first.block.created_at == _SECOND
    assert first.cursor.created_at == _FIRST
