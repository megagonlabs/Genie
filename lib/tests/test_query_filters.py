"""Tests for search queries and portable filter behavior."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest

from genie import HookContext, InMemoryBackend, Memory, MemoryBlock, MemoryHook, MemoryQuery, Scope, SearchResult
from genie.query.filters import matches_filters, normalize_filters


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
    """The short filter operators, such as `>`, `<=`, `in`, and `!=`, select the correct blocks."""

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
    assert memory.search(filters={"owner": {"in": 3}}) == []


def test_unknown_filter_operators_raise_before_any_backend_runs() -> None:
    """A mistyped operator raises `ValueError` and does not return an empty result."""

    memory = Memory()
    memory.put(type="note", value="note", metadata={"priority": 3})

    assert len(memory.search(filters={"priority": {">=": 2}})) == 1
    with pytest.raises(ValueError, match=r"filter operator '>>=' on field 'priority' is not supported"):
        memory.search(filters={"priority": {">>=": 2}})
    with pytest.raises(ValueError, match=r"Supported operators: ==, =, !=, >, >=, <, <=, in, \$eq"):
        MemoryQuery(filters={"metadata.priority": {"$unknown": 2}})
    with pytest.raises(ValueError, match="'between'"):
        normalize_filters({"priority": {"$gte": 1, "between": (1, 3)}})

    # The matcher has its own guard for adapters that do not normalize the filters.
    block = MemoryBlock(type="note", value="note", metadata={"priority": 3})
    assert matches_filters(block, {"priority": {"$unknown": 2}}) is False


def test_canonical_filter_operators_still_work() -> None:
    """`Memory.search` still accepts the canonical operators that adapters receive, such as `$gte` and `$lte`."""

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
    """The table has a positive case and a negative case for every canonical comparison operator."""

    memory = Memory()
    block = memory.put(
        type="note",
        value="canonical",
        metadata={"priority": 2, "owner": "team"},
    )

    results = memory.search(filters={field: condition})

    assert ([result.block.id for result in results] == [block.id]) is matches


def test_filter_aliases_match_block_fields_and_do_not_mutate_inputs() -> None:
    """Filter aliases match block fields, and `normalize_filters` copies its input and does not change it."""

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
            """Record the filters, then search through the in-memory backend."""

            self.seen_filters = query.filters
            return super().search(query)

    backend = RecordingBackend()
    memory = Memory(backend=backend)
    memory.put(type="note", value="normalized", metadata={"priority": 2})

    results = memory.search(filters={"metadata.priority": {">=": 2, "<=": 3}})

    assert [result.block.value for result in results] == ["normalized"]
    assert backend.seen_filters == {"metadata.priority": {"$gte": 2, "$lte": 3}}


def test_query_accepts_tuple_types_and_string_scope() -> None:
    """`MemoryQuery` normalizes string scopes and preserves tuple types."""

    query = MemoryQuery(scope="subtree", types=("note",))

    assert isinstance(query.scope, Scope)
    assert query.scope == Scope.SUBTREE
    assert query.types == ("note",)


def test_query_validation_and_immutability() -> None:
    """`MemoryQuery` rejects invalid scopes and remains immutable after creation."""

    query = MemoryQuery(scope="links", types=["note", "task"])

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
    """`MemoryQuery` raises an error for a malformed field before the query reaches a backend adapter."""

    with pytest.raises(error):
        MemoryQuery(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"root_id": ""}, "MemoryQuery root_id must not be empty"),
        ({"root_id": "  "}, "MemoryQuery root_id must not be empty"),
        ({"types": ("note", "")}, "MemoryQuery types entry must not be empty"),
        ({"types": [" \t"]}, "MemoryQuery types entry must not be empty"),
    ],
)
def test_query_rejects_empty_root_id_and_type_entries(kwargs: dict[str, Any], message: str) -> None:
    """`MemoryQuery` raises `ValueError` for an empty root id or `types` entry, as the rest of the API does for ids and types."""

    with pytest.raises(ValueError, match=message):
        MemoryQuery(**kwargs)


def test_search_refuses_empty_root_id_and_type_entries_before_any_backend_runs() -> None:
    """`Memory.search`, `MemoryHandle.search`, and a `before_search` hook cannot send an empty root id or type to the backend."""

    class RecordingBackend(InMemoryBackend):
        """Backend that counts the searches it receives."""

        def __init__(self) -> None:
            super().__init__()
            self.searches = 0

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            """Count the search, then search through the in-memory backend."""

            self.searches += 1
            return super().search(query)

    class EmptyRootHook(MemoryHook):
        """Hook that tries to replace the search root with an empty id."""

        def before_search(self, context: HookContext, query: MemoryQuery) -> MemoryQuery:
            """Return the query with an empty root id."""

            return replace(query, root_id="")

    backend = RecordingBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="note", value="root")
    handle = memory.get_handle(root=root)

    with pytest.raises(ValueError, match="types entry must not be empty"):
        memory.search("root", types=["note", ""])
    with pytest.raises(ValueError, match="types entry must not be empty"):
        handle.search("root", types=[" "])
    memory.hooks.register(EmptyRootHook())
    with pytest.raises(ValueError, match="root_id must not be empty"):
        memory.search("root")
    assert backend.searches == 0


def test_memory_search_preserves_query_validation_at_the_public_boundary() -> None:
    """`Memory.search` raises `TypeError` for a wrong `types` or `filters` argument and does not convert it silently."""

    memory = Memory()

    with pytest.raises(TypeError, match="types must be a list or tuple"):
        memory.search(types=cast(Any, "note"))
    with pytest.raises(TypeError, match="filters must be a dictionary"):
        memory.search(filters=cast(Any, []))


def test_query_copies_nested_filter_operands_and_type_lists() -> None:
    """A change to the lists of the caller cannot change a query after construction."""

    owners = ["team"]
    types = ["note"]
    filters = {"owner": {"$in": owners}}
    query = MemoryQuery(types=types, filters=filters)

    owners.append("other")
    types.append("task")

    assert query.types == ("note",)
    assert query.filters == {"owner": {"$in": ["team"]}}


def test_filter_fields_use_declared_block_fields_before_metadata_fallback() -> None:
    """A `MemoryBlock` method name does not hide a metadata key with the same name."""

    memory = Memory()
    block = memory.put(
        type="note",
        value="value",
        metadata={"with_updates": "metadata-value"},
    )

    results = memory.search(filters={"with_updates": "metadata-value"})

    assert [result.block.id for result in results] == [block.id]


def test_portable_in_filters_require_a_collection_and_empty_operators_fail_closed() -> None:
    """The portable matcher never treats a string `$in` operand or an empty operator map as a match."""

    memory = Memory()
    memory.put(type="note", value="value", metadata={"owner": "team"})

    assert memory.search(filters={"owner": {"$in": "other-team"}}) == []
    assert memory.search(filters={"owner": {}}) == []


def test_portable_filter_matching_fails_closed_for_non_string_keys() -> None:
    """`matches_filters` returns `False` for a field key or an operator key that is not a string."""

    block = MemoryBlock(type="note", value="value", metadata={"owner": "team"})

    assert matches_filters(block, cast(dict[str, Any], {1: "team"})) is False
    assert matches_filters(
        block,
        {"owner": cast(dict[str, Any], {1: "team"})},
    ) is False


def test_non_global_search_requires_an_explicit_root() -> None:
    """A scoped search without a root raises `ValueError` and does not become a global scan."""

    memory = Memory()
    memory.put(type="note", value="visible only to a deliberate query")

    with pytest.raises(ValueError, match="requires a root block"):
        memory.search(scope=Scope.SUBTREE)


def test_search_results_validate_backend_facing_record_fields() -> None:
    """`SearchResult` raises an error for a malformed field, so a bad record cannot reach the rank and policy steps."""

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


class _CountingBackend(InMemoryBackend):
    """In-memory backend that records every point read it serves."""

    def __init__(self) -> None:
        """Start with an empty read log."""

        super().__init__()
        self.reads: list[str] = []

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Record the read, then answer it normally."""

        self.reads.append(block_id)
        return super().get_block(block_id)


def test_search_reads_each_result_block_once() -> None:
    """`Memory.search` checks the stored block of a result once, not once per pass.

    - **Two passes, one verdict.** `Memory.search` filters visibility twice,
      before and after the `after_search` hooks, because a hook can add
      results. Both passes share one verdict for each block id, so the second
      pass costs no extra reads.
    - **Cost.** On a server-backed adapter, this halves the round trips of a
      search. That cost grows with the number of results.
    """

    backend = _CountingBackend()
    memory = Memory(backend=backend, principal="owner")
    root = memory.put(type="root", value="workspace")
    notes = [
        memory.put(parent=root, type="note", value=f"needle {index}")
        for index in range(5)
    ]
    backend.reads.clear()

    results = memory.search("needle", root=root, scope=Scope.SUBTREE)
    returned = [result.block.id for result in results]

    assert sorted(returned) == sorted(note.id for note in notes)
    # Exactly 1 read for each returned block. The reads of the query root are
    # part of the authorization path, not of the result filter, so the count
    # excludes them.
    result_reads = [block_id for block_id in backend.reads if block_id in set(returned)]
    assert sorted(result_reads) == sorted(returned)


def test_search_visibility_is_decided_again_for_every_search() -> None:
    """A shared verdict applies to one search only, never to two searches.

    A reused verdict reads a stale store, so a block deleted between two
    searches does not appear in the second result.
    """

    memory = Memory(principal="owner")
    root = memory.put(type="root", value="workspace")
    note = memory.put(parent=root, type="note", value="needle")

    first = memory.search("needle", root=root, scope=Scope.SUBTREE)
    assert [result.block.id for result in first] == [note.id]

    memory.delete(note)
    second = memory.search("needle", root=root, scope=Scope.SUBTREE)

    assert second == []
