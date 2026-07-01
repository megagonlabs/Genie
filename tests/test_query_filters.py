"""Tests for search queries and portable filter behavior."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from genie import InMemoryBackend, Memory, MemoryQuery, Scope, SearchResult


def test_search_filters_types_scopes_and_limits() -> None:
    """Search combines text, type filters, subtree roots, and limits."""

    memory = Memory()
    root = memory.put(type="root", value="workspace")
    high = memory.put(
        parent=root,
        type="note",
        value="alpha alpha",
        metadata={"priority": 3, "owner": "team"},
    )
    memory.put(
        parent=root,
        type="note",
        value="alpha",
        metadata={"priority": 1, "owner": "team"},
    )
    memory.put(
        parent=root,
        type="decision",
        value="alpha",
        metadata={"priority": 5, "owner": "team"},
    )

    results = memory.search(
        "alpha",
        root=root,
        scope="subtree",
        types=["note"],
        filters={"metadata.priority": {">=": 2}, "owner": "team"},
        limit=1,
    )

    assert [result.block.id for result in results] == [high.id]
    assert results[0].score == 2.0
    assert [result.block.id for result in memory.search(root=high, scope="self")] == [high.id]
    assert memory.search("alpha", limit=0) == []

    with pytest.raises(ValueError):
        memory.search("alpha", limit=-1)


def test_filter_operators_cover_common_comparisons() -> None:
    """Human-friendly filter operators behave as expected."""

    memory = Memory()
    memory.put(type="note", value="low", metadata={"priority": 1, "owner": "team"})
    mid = memory.put(type="note", value="mid", metadata={"priority": 2, "owner": "team"})
    memory.put(type="note", value="high", metadata={"priority": 3, "owner": "other"})

    results = memory.search(
        types=["note"],
        filters={
            "metadata.priority": {">": 1, "<=": 2},
            "owner": {"in": ["team"], "!=": "other"},
        },
    )

    assert [result.block.id for result in results] == [mid.id]
    assert memory.search(filters={"metadata.priority": {"$unknown": 2}}) == []
    assert memory.search(filters={"owner": {"in": 3}}) == []


def test_canonical_filter_operators_still_work() -> None:
    """Canonical adapter-facing filter operators remain supported."""

    memory = Memory()
    block = memory.put(type="note", value="canonical", metadata={"priority": 2})

    results = memory.search(filters={"metadata.priority": {"$gte": 2, "$lte": 2}})

    assert [result.block.id for result in results] == [block.id]


def test_backends_receive_normalized_filter_operators() -> None:
    """Backends receive normalized canonical filter operators."""

    class RecordingBackend(InMemoryBackend):
        """Backend that records the filters it receives."""

        def __init__(self) -> None:
            super().__init__()
            self.seen_filters: dict[str, Any] | None = None

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            """Record filters before delegating to the in-memory backend."""

            self.seen_filters = query.filters
            return super().search(query)

    backend = RecordingBackend()
    memory = Memory(backend=backend)
    memory.put(type="note", value="normalized", metadata={"priority": 2})

    results = memory.search(filters={"metadata.priority": {">=": 2, "<=": 3}})

    assert [result.block.value for result in results] == ["normalized"]
    assert backend.seen_filters == {"metadata.priority": {"$gte": 2, "$lte": 3}}


def test_query_accepts_tuple_types_and_string_scope() -> None:
    """MemoryQuery normalizes string scopes and preserves tuple types."""

    query = MemoryQuery(scope="subtree", types=("note",))

    assert query.scope == Scope.SUBTREE
    assert query.types == ("note",)


def test_query_validation_and_immutability() -> None:
    """MemoryQuery rejects invalid scopes and remains immutable after creation."""

    query = MemoryQuery(scope="links", types=cast(Any, ["note", "task"]))

    assert query.scope == Scope.LINKS
    assert query.types == ("note", "task")
    with pytest.raises(ValueError):
        MemoryQuery(scope=cast(Any, "sideways"))
    with pytest.raises(FrozenInstanceError):
        query.limit = 5  # type: ignore[misc]


def test_filter_incompatible_comparisons_fail_closed() -> None:
    """Incompatible filter comparisons produce no matches."""

    memory = Memory()
    memory.put(type="note", value="note", metadata={"priority": "high"})

    assert memory.search(filters={"metadata.priority": {">": 1}}) == []
