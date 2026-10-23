"""Tests for temporal collections and optional temporal indexes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest

from genie import (
    AccessDenied,
    InMemoryBackend,
    Memory,
    MemoryConflict,
    MemoryNotFound,
    MemoryPolicy,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.structures import (
    TemporalAccess,
    TemporalCollection,
    TemporalEntry,
    TemporalItem,
    ValidityInterval,
)


class ScanOnlyTemporalStore:
    """Expose the `TemporalStore` methods without the optional `TemporalIndex` protocol."""

    def __init__(self, backend: InMemoryBackend) -> None:
        """Wrap the reference backend."""

        self.backend = backend

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Delegate temporal insertion."""

        return self.backend.add_temporal_entry(entry)

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Delegate temporal replacement."""

        return self.backend.replace_temporal_entry(entry)

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Delegate exact temporal lookup."""

        return self.backend.get_temporal_entry(collection_id, block_id)

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Delegate `list_temporal_entries` to the backend."""

        return self.backend.list_temporal_entries(
            collection_id,
            after_block_id=after_block_id,
            limit=limit,
        )

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Delegate temporal removal."""

        self.backend.remove_temporal_entry(collection_id, block_id)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Delegate temporal cleanup."""

        self.backend.delete_temporal_entries_for_block(block_id)


class RecordingTemporalIndex:
    """Count index calls and delegate each query to the backend."""

    def __init__(self, backend: InMemoryBackend) -> None:
        """Wrap the reference backend's temporal query implementation."""

        self.backend = backend
        self.calls = 0

    def query_temporal_entries(
        self,
        collection_id: str,
        *,
        occurred_only: bool = False,
        occurred_start: datetime | None = None,
        occurred_stop: datetime | None = None,
        valid_at: datetime | None = None,
        overlaps: ValidityInterval | None = None,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Record and delegate one indexed temporal query."""

        self.calls += 1
        return self.backend.query_temporal_entries(
            collection_id,
            occurred_only=occurred_only,
            occurred_start=occurred_start,
            occurred_stop=occurred_stop,
            valid_at=valid_at,
            overlaps=overlaps,
            after_block_id=after_block_id,
            limit=limit,
        )


def test_temporal_collection_adds_replaces_and_removes_membership() -> None:
    """Temporal membership is unique and separate from the referenced block."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="facts")
    other_root = memory.put(type="temporal_collection", value="other")
    fact = memory.put(type="fact", value="Alice lives in Seattle")
    collection = TemporalCollection(memory, backend, collection=root)
    other = TemporalCollection(memory, backend, collection=other_root)
    occurred_at = _time(10)

    added = collection.add(fact, occurred_at=occurred_at)

    assert added.entry.occurred_at == occurred_at
    assert added.block == fact
    assert collection.get(fact) == added
    with pytest.raises(MemoryConflict, match="already"):
        collection.add(fact, occurred_at=_time(11))

    validity = ValidityInterval(_time(1), _time(20))
    replaced = collection.replace(fact, validity=validity)
    assert replaced.entry.occurred_at is None
    assert replaced.entry.validity == validity
    other.add(fact, occurred_at=_time(12))

    collection.remove(fact)
    assert collection.get(fact) is None
    assert other.get(fact) is not None
    assert memory.get(fact) == fact
    with pytest.raises(MemoryConflict, match="is not in"):
        collection.remove(fact)


def test_temporal_collection_filters_events_and_half_open_validity() -> None:
    """Event, valid-at, and overlap queries use half-open time boundaries."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    collection = TemporalCollection(memory, backend, collection=root)
    early = memory.put(type="event", value="early", id="mem_a")
    current = memory.put(type="fact", value="current", id="mem_b")
    old = memory.put(type="fact", value="old", id="mem_c")
    future = memory.put(type="fact", value="future", id="mem_d")
    late = memory.put(type="event", value="late", id="mem_e")
    collection.add(early, occurred_at=_time(1))
    collection.add(
        current,
        occurred_at=_time(10),
        validity=ValidityInterval(_time(5), _time(20)),
    )
    collection.add(old, validity=ValidityInterval(_time(1), _time(10)))
    collection.add(future, validity=ValidityInterval(_time(20)))
    collection.add(late, occurred_at=_time(20))

    assert _values(collection.events()) == ["early", "current", "late"]
    assert _values(collection.events(start=_time(10), stop=_time(20))) == [
        "current"
    ]
    assert _values(collection.valid_at(_time(10))) == ["current"]
    assert _values(collection.valid_at(_time(20))) == ["future"]
    assert _values(
        collection.overlapping(ValidityInterval(_time(10), _time(20)))
    ) == ["current"]
    assert _values(
        collection.scan(
            occurred_start=_time(10),
            occurred_stop=_time(20),
            valid_at=_time(15),
        )
    ) == ["current"]
    assert collection.scan(limit=0) == []
    assert collection.events(limit=0) == []


def test_portable_scan_and_optional_index_return_the_same_results() -> None:
    """An optional index changes candidate selection, not query semantics."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    scan_store = ScanOnlyTemporalStore(backend)
    portable = TemporalCollection(memory, scan_store, collection=root)
    selected = memory.put(type="event", value="selected", id="mem_a")
    ignored = memory.put(type="event", value="ignored", id="mem_b")
    portable.add(selected, occurred_at=_time(10))
    portable.add(ignored, occurred_at=_time(30))
    index = RecordingTemporalIndex(backend)
    indexed = TemporalCollection(
        memory,
        scan_store,
        collection=root,
        index=index,
    )

    expected = portable.events(start=_time(5), stop=_time(20))
    actual = indexed.events(start=_time(5), stop=_time(20))

    assert actual == expected
    assert _values(actual) == ["selected"]
    assert index.calls == 1


def test_temporal_collection_reads_across_bounded_portable_pages() -> None:
    """A portable scan reads more than one store page to return the full `limit` of visible items."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    collection = TemporalCollection(
        memory,
        ScanOnlyTemporalStore(backend),
        collection=root,
    )
    for number in range(125):
        event = memory.put(
            type="event",
            value=number,
            id=f"mem_{number:03d}",
        )
        collection.add(event, occurred_at=_time(1))

    items = collection.events(limit=125)

    assert len(items) == 125
    assert [item.block.value for item in items] == list(range(125))


def test_temporal_collection_preserves_handle_scope_and_policy() -> None:
    """For a scoped viewer, `get` raises `AccessDenied` for a private or outside block, and scans omit those blocks."""

    backend = InMemoryBackend()
    owner = Memory(backend=backend, principal="agent:owner")
    root = owner.put(
        type="temporal_collection",
        value="timeline",
        policy=MemoryPolicy.public(),
    )
    visible = owner.put(
        parent=root,
        type="event",
        value="visible",
        policy=MemoryPolicy.public(),
    )
    private = owner.put(parent=root, type="event", value="private")
    outside = owner.put(type="event", value="outside", policy=MemoryPolicy.public())
    full = TemporalCollection(owner, backend, collection=root)
    full.add(visible, occurred_at=_time(1))
    full.add(private, occurred_at=_time(2))
    full.add(outside, occurred_at=_time(3))
    scoped = TemporalCollection(
        owner.get_handle(root=root, principal="agent:viewer"),
        backend,
        collection=root,
    )

    assert _values(scoped.events()) == ["visible"]
    with pytest.raises(AccessDenied):
        scoped.get(private)
    with pytest.raises(AccessDenied):
        scoped.get(outside)
    with pytest.raises(AccessDenied):
        scoped.add(outside, occurred_at=_time(4))


def test_temporal_get_checks_the_block_before_the_membership_lookup() -> None:
    """`get` refuses a hidden block in the same way if it is a member or not, so it does not show membership."""

    backend = InMemoryBackend()
    owner = Memory(backend=backend, principal="agent:owner")
    root = owner.put(type="temporal_collection", value="timeline", policy=MemoryPolicy.public())
    private = owner.put(parent=root, type="event", value="private, not a member")
    outside_member = owner.put(type="event", value="outside member", policy=MemoryPolicy.public())
    outside_other = owner.put(type="event", value="outside other", policy=MemoryPolicy.public())
    TemporalCollection(owner, backend, collection=root).add(outside_member, occurred_at=_time(1))
    scoped = TemporalCollection(owner.get_handle(root=root, principal="agent:viewer"), backend, collection=root)
    viewer = TemporalCollection(Memory(backend=backend, principal="agent:viewer"), backend, collection=root)

    for block in (outside_member.id, outside_other.id, "missing"):
        with pytest.raises(AccessDenied, match="outside memory handle root"):
            scoped.get(block)
    with pytest.raises(AccessDenied):
        viewer.get(private)
    assert viewer.get(outside_other) is None
    assert viewer.get("missing") is None
    member = viewer.get(outside_member)
    assert member is not None
    assert member.block.id == outside_member.id


def test_temporal_entries_are_cleaned_when_blocks_are_deleted() -> None:
    """A delete of a target block or of the collection block removes the temporal state that depends on it."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    event = memory.put(type="event", value="event")
    collection = TemporalCollection(memory, backend, collection=root)
    collection.add(event, occurred_at=_time(1))

    memory.delete(event)
    assert collection.get(event.id) is None

    replacement = memory.put(type="event", value="replacement")
    collection.add(replacement, occurred_at=_time(2))
    memory.delete(root)
    assert backend.list_temporal_entries(root.id) == []
    with pytest.raises(MemoryNotFound):
        _ = collection.collection


def test_temporal_collection_handles_a_stale_backend_entry_defensively() -> None:
    """If a backend keeps a stale entry, `get` raises `MemoryNotFound` and `scan` omits the deleted target."""

    class StaleTemporalBackend(InMemoryBackend):
        """Simulate an adapter that leaves temporal entries after target deletion."""

        def delete_temporal_entries_for_block(self, block_id: str) -> None:
            """Keep the temporal entries on purpose so that the test can check stale state."""

    backend = StaleTemporalBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    target = memory.put(type="event", value="stale")
    collection = TemporalCollection(memory, backend, collection=root)
    collection.add(target, occurred_at=_time(1))
    memory.delete(target)

    with pytest.raises(MemoryNotFound, match="references missing memory block"):
        collection.get(target)
    assert collection.scan() == []


def test_portable_temporal_scan_covers_negative_half_open_boundaries() -> None:
    """Portable scans exclude entries that have no event time or a validity interval outside the half-open bounds."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    collection = TemporalCollection(
        memory,
        ScanOnlyTemporalStore(backend),
        collection=root,
    )
    early = memory.put(type="event", value="early", id="mem_a")
    at_stop = memory.put(type="event", value="at-stop", id="mem_b")
    expired = memory.put(type="fact", value="expired", id="mem_c")
    future = memory.put(type="fact", value="future", id="mem_d")
    timeless = memory.put(type="fact", value="timeless", id="mem_e")
    collection.add(early, occurred_at=_time(1))
    collection.add(at_stop, occurred_at=_time(10))
    collection.add(expired, validity=ValidityInterval(_time(1), _time(5)))
    collection.add(future, validity=ValidityInterval(_time(10), _time(20)))
    collection.add(timeless, validity=ValidityInterval(_time(1)))

    assert collection.events(start=_time(5), stop=_time(10)) == []
    assert collection.valid_at(_time(5)) == [
        collection.get(timeless)
    ]
    assert collection.overlapping(ValidityInterval(_time(5), _time(10))) == [
        collection.get(timeless)
    ]
    assert collection.overlapping(ValidityInterval(_time(20), _time(21))) == [
        collection.get(timeless)
    ]


@pytest.mark.parametrize(
    "operation",
    [
        lambda collection: collection.events(
            start=datetime(2026, 1, 1)  # noqa: DTZ001 - invalid input
        ),
        lambda collection: collection.events(start=cast(datetime, "bad")),
        lambda collection: collection.events(start=_time(2), stop=_time(1)),
        lambda collection: collection.valid_at(
            datetime(2026, 1, 1)  # noqa: DTZ001 - invalid input
        ),
        lambda collection: collection.scan(overlaps=cast(ValidityInterval, "bad")),
        lambda collection: collection.scan(limit=-1),
        lambda collection: collection.scan(limit=True),
    ],
)
def test_temporal_collection_rejects_invalid_queries(
    operation: Callable[[TemporalCollection], object],
) -> None:
    """Invalid temporal bounds and limits raise `TypeError` or `ValueError`."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="temporal_collection", value="timeline")
    collection = TemporalCollection(memory, backend, collection=root)

    assert collection.scan() == []

    with pytest.raises((TypeError, ValueError)):
        operation(collection)


def test_temporal_collection_requires_store_and_valid_optional_index() -> None:
    """The constructor raises `UnsupportedOperation` for a store or an index that does not support the temporal protocols."""

    memory = Memory()
    root = memory.put(type="temporal_collection", value="timeline")
    store = memory.backend
    assert isinstance(store, TemporalStore)

    with pytest.raises(UnsupportedOperation):
        TemporalCollection(memory, object(), collection=root)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedOperation):
        TemporalCollection(
            memory,
            store,
            collection=root,
            index=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(MemoryNotFound, match="Temporal collection block"):
        TemporalCollection(memory, store, collection="missing")

    collection = TemporalCollection(memory, store, collection=root)
    with pytest.raises(MemoryNotFound, match="Memory block 'missing'"):
        collection.add("missing", occurred_at=_time(1))


def test_memory_and_handle_implement_temporal_access() -> None:
    """`Memory` and `MemoryHandle` satisfy `TemporalAccess`, and the reference backend satisfies `TemporalStore` and `TemporalIndex`."""

    memory = Memory()
    root = memory.put(type="temporal_collection", value="timeline")

    assert isinstance(memory, TemporalAccess)
    assert isinstance(memory.get_handle(root=root), TemporalAccess)
    assert isinstance(memory.backend, TemporalStore)
    assert isinstance(memory.backend, TemporalIndex)


def _time(day: int) -> datetime:
    """Return a deterministic UTC time in January 2026."""

    return datetime(2026, 1, day, tzinfo=UTC)


def _values(items: list[TemporalItem]) -> list[object]:
    """Return block values from temporal items."""

    return [item.block.value for item in items]
