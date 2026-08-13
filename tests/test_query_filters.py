"""Tests for search queries and portable filter behavior."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
from typing import Any, cast

import pytest

from genie import InMemoryBackend, Memory, MemoryBlock, MemoryQuery, Scope, SearchResult
from genie.query.filters import normalize_filters


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


@pytest.mark.parametrize(
    ("field", "condition", "matches"),
    [
        ("owner", {"$eq": "team"}, True),
        ("owner", {"$eq": "other"}, False),
        ("owner", {"$ne": "other"}, True),
        ("owner", {"$ne": "team"}, False),
        ("owner", {"$in": ["team", "other"]}, True),
        ("owner", {"$in": ["other"]}, False),
        ("metadata.priority", {"$gt": 1}, True),
        ("metadata.priority", {"$gt": 2}, False),
        ("metadata.priority", {"$gte": 2}, True),
        ("metadata.priority", {"$gte": 3}, False),
        ("metadata.priority", {"$lt": 3}, True),
        ("metadata.priority", {"$lt": 2}, False),
        ("metadata.priority", {"$lte": 2}, True),
        ("metadata.priority", {"$lte": 1}, False),
    ],
)
def test_canonical_filter_operator_truth_table(
    field: str,
    condition: dict[str, object],
    matches: bool,
) -> None:
    """Every canonical comparison operator includes positive and negative cases."""

    memory = Memory()
    block = memory.put(
        type="note",
        value="canonical",
        metadata={"priority": 2, "owner": "team"},
    )

    results = memory.search(filters={field: condition})

    assert ([result.block.id for result in results] == [block.id]) is matches


def test_filter_aliases_match_block_fields_and_do_not_mutate_inputs() -> None:
    """Filter aliases work for block fields while normalization copies inputs."""

    memory = Memory()
    wanted = memory.put(type="note", value="wanted", metadata={"priority": 2})
    memory.put(type="task", value="task", metadata={"priority": 2})
    filters = {
        "type": {"=": "note"},
        "metadata.priority": {"==": 2},
    }

    normalized = normalize_filters(filters)
    results = memory.search(filters=filters)

    assert normalized == {"type": {"$eq": "note"}, "metadata.priority": {"$eq": 2}}
    assert filters == {"type": {"=": "note"}, "metadata.priority": {"==": 2}}
    assert [result.block.id for result in results] == [wanted.id]


def test_backends_receive_normalized_filter_operators() -> None:
    """Backends receive normalized canonical filter operators."""

    class RecordingBackend(InMemoryBackend):
        """Backend that records the filters it receives."""

        def __init__(self) -> None:
            super().__init__()
            self.seen_filters: dict[str, Any] | None = None

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            """Record filters before calling the in-memory backend."""

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


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"text": 1}, TypeError),
        ({"root_id": 1}, TypeError),
        ({"types": "note"}, TypeError),
        ({"types": ("note", 1)}, TypeError),
        ({"filters": []}, TypeError),
        ({"filters": {1: "value"}}, TypeError),
        ({"filters": {"owner": {1: "value"}}}, TypeError),
        ({"limit": True}, TypeError),
        ({"limit": 1.5}, TypeError),
    ],
)
def test_query_rejects_invalid_runtime_field_types(
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    """Malformed query fields fail before they reach a backend adapter."""

    with pytest.raises(error):
        MemoryQuery(**kwargs)


def test_memory_search_preserves_query_validation_at_the_public_boundary() -> None:
    """Friendly search arguments are validated instead of silently coerced."""

    memory = Memory()

    with pytest.raises(TypeError, match="types must be a list or tuple"):
        memory.search(types=cast(Any, "note"))
    with pytest.raises(TypeError, match="filters must be a dictionary"):
        memory.search(filters=cast(Any, []))


def test_query_copies_nested_filter_operands_and_type_lists() -> None:
    """Caller mutation cannot change a query after construction."""

    owners = ["team"]
    types = ["note"]
    filters = {"owner": {"$in": owners}}
    query = MemoryQuery(types=types, filters=filters)

    owners.append("other")
    types.append("task")

    assert query.types == ("note",)
    assert query.filters == {"owner": {"$in": ["team"]}}


def test_filter_fields_use_declared_block_fields_before_metadata_fallback() -> None:
    """Methods do not shadow metadata keys that happen to share their names."""

    memory = Memory()
    block = memory.put(
        type="note",
        value="value",
        metadata={"with_updates": "metadata-value"},
    )

    results = memory.search(filters={"with_updates": "metadata-value"})

    assert [result.block.id for result in results] == [block.id]


def test_portable_in_filters_require_a_collection_and_empty_operators_fail_closed() -> None:
    """Portable matching never treats strings or empty operator maps as matches."""

    memory = Memory()
    memory.put(type="note", value="value", metadata={"owner": "team"})

    assert memory.search(filters={"owner": {"$in": "other-team"}}) == []
    assert memory.search(filters={"owner": {}}) == []


def test_non_global_search_requires_an_explicit_root() -> None:
    """A missing scoped-search root cannot silently widen into a global scan."""

    memory = Memory()
    memory.put(type="note", value="visible only to a deliberate query")

    with pytest.raises(ValueError, match="requires a root block"):
        memory.search(scope=Scope.SUBTREE)


def test_search_results_validate_backend_facing_record_fields() -> None:
    """Malformed result records fail before ranking or policy filtering."""

    block = MemoryBlock(type="note", value="value")

    assert SearchResult(block, score=2).score == 2.0
    with pytest.raises(TypeError, match="block must be a MemoryBlock"):
        SearchResult(cast(MemoryBlock, object()))
    with pytest.raises(TypeError, match="score must be a number"):
        SearchResult(block, score=cast(float, True))
    with pytest.raises(ValueError, match="score must be finite"):
        SearchResult(block, score=math.nan)
    with pytest.raises(TypeError, match="reason must be a string"):
        SearchResult(block, reason=cast(str, 1))
