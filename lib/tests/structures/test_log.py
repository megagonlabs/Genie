"""Tests for ordered event logs and their atomic backend capability."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from genie import (
    AccessDenied,
    InMemoryBackend,
    Memory,
    MemoryNotFound,
    SequenceStore,
    UnsupportedOperation,
)
from genie.structures import EventLog, SequenceAccess


def test_event_log_appends_and_reads_stable_sequences() -> None:
    """`append` gives each entry a stable position, and range reads use half-open bounds."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="conversation")
    events = [memory.put(type="event", value=index) for index in range(5)]
    log = EventLog(memory, backend, log=root)

    appended = [log.append(event) for event in events]

    assert [item.entry.sequence for item in appended] == [0, 1, 2, 3, 4]
    assert [item.block.value for item in log.read()] == [0, 1, 2, 3, 4]
    assert [item.block.value for item in log.read(start=1, stop=4)] == [1, 2, 3]
    assert [item.block.value for item in log.read(reverse=True, limit=2)] == [4, 3]
    assert [item.block.value for item in log.tail(limit=2)] == [3, 4]
    assert log.get(2) == appended[2]
    assert log.get(9) is None
    assert log.read(limit=0) == []
    assert log.tail(limit=0) == []


def test_event_log_can_append_the_same_block_more_than_once() -> None:
    """A log records each occurrence of a block, so the same block can have 2 positions."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    event = memory.put(type="event", value="reused")
    log = EventLog(memory, backend, log=root)

    first = log.append(event)
    second = log.append(event)

    assert first.entry.sequence == 0
    assert second.entry.sequence == 1
    assert first.block.id == second.block.id


def test_event_log_reads_across_bounded_backend_pages() -> None:
    """The facade reads more than one store page to return the full `limit` of visible items."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    event = memory.put(type="event", value="shared")
    log = EventLog(memory, backend, log=root)
    for _ in range(125):
        log.append(event)

    items = log.read(limit=125)

    assert [item.entry.sequence for item in items] == list(range(125))


def test_event_log_does_not_reuse_sequences_after_target_deletion() -> None:
    """Cleanup can leave a gap, but each later append still gets a higher position."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    first = memory.put(type="event", value="first")
    second = memory.put(type="event", value="second")
    log = EventLog(memory, backend, log=root)

    assert log.append(first).entry.sequence == 0
    memory.delete(first)
    assert log.append(second).entry.sequence == 1
    assert [item.entry.sequence for item in log.read()] == [1]


def test_event_log_preserves_handle_scope() -> None:
    """In a scoped log, `get` and `append` raise `AccessDenied` for an outside block, and `read` omits it."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    inside = memory.put(parent=root, type="event", value="inside")
    outside = memory.put(type="event", value="outside")
    full = EventLog(memory, backend, log=root)
    full.append(inside)
    outside_item = full.append(outside)
    scoped = EventLog(memory.get_handle(root=root), backend, log=root)

    assert [item.block for item in scoped.read()] == [inside]
    with pytest.raises(AccessDenied):
        scoped.get(outside_item.entry.sequence)
    with pytest.raises(AccessDenied):
        scoped.append(outside)


def test_event_log_append_is_atomic_under_concurrent_writers() -> None:
    """100 concurrent appends get unique positions that fill 0 to 99 with no gap."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    event = memory.put(type="event", value="shared")
    log = EventLog(memory, backend, log=root)

    with ThreadPoolExecutor(max_workers=12) as executor:
        items = list(executor.map(lambda _: log.append(event), range(100)))

    assert sorted(item.entry.sequence for item in items) == list(range(100))
    assert [entry.sequence for entry in backend.list_log_entries(root.id)] == list(
        range(100)
    )


def test_event_log_cleanup_removes_deleted_log_state() -> None:
    """A delete of the log block removes its entries and its sequence allocator."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    event = memory.put(type="event", value="event")
    log = EventLog(memory, backend, log=root)
    log.append(event)

    memory.delete(root)

    assert backend.list_log_entries(root.id) == []
    with pytest.raises(MemoryNotFound):
        _ = log.log


def test_event_log_handles_a_stale_backend_entry_defensively() -> None:
    """If a backend keeps a stale entry, `get` raises `MemoryNotFound` and `read` omits the deleted target."""

    class StaleLogBackend(InMemoryBackend):
        """Simulate an adapter that leaves log entries after target deletion."""

        def delete_log_entries_for_block(self, block_id: str) -> None:
            """Keep the log entries on purpose so that the test can check stale state."""

    backend = StaleLogBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    target = memory.put(type="event", value="stale")
    log = EventLog(memory, backend, log=root)
    appended = log.append(target)
    memory.delete(target)

    with pytest.raises(MemoryNotFound, match="references missing memory block"):
        log.get(appended.entry.sequence)
    assert log.read() == []


@pytest.mark.parametrize(
    "operation",
    [
        lambda log: log.get(-1),
        lambda log: log.get(cast(int, True)),
        lambda log: log.read(start=-1),
        lambda log: log.read(stop=-1),
        lambda log: log.read(start=3, stop=2),
        lambda log: log.read(limit=-1),
        lambda log: log.read(limit=True),
        lambda log: log.read(reverse=cast(bool, 1)),
    ],
)
def test_event_log_rejects_invalid_ranges(
    operation: Callable[[EventLog], object],
) -> None:
    """Invalid positions, ranges, limits, and flags raise `TypeError` or `ValueError`."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="log", value="events")
    log = EventLog(memory, backend, log=root)

    with pytest.raises((TypeError, ValueError)):
        operation(log)


def test_event_log_requires_the_optional_backend_capability() -> None:
    """The constructor raises `UnsupportedOperation` for a backend without sequence operations."""

    memory = Memory()
    root = memory.put(type="log", value="events")
    store = memory.backend
    assert isinstance(store, SequenceStore)

    with pytest.raises(UnsupportedOperation):
        EventLog(memory, object(), log=root)  # type: ignore[arg-type]

    with pytest.raises(MemoryNotFound, match="Event log block"):
        EventLog(memory, store, log="missing")

    log = EventLog(memory, store, log=root)
    with pytest.raises(MemoryNotFound, match="Memory block 'missing'"):
        log.append("missing")


def test_memory_and_handle_implement_sequence_access() -> None:
    """`Memory` and `MemoryHandle` both satisfy `SequenceAccess`, and the reference backend satisfies `SequenceStore`."""

    memory = Memory()
    root = memory.put(type="root", value="root")

    assert isinstance(memory, SequenceAccess)
    assert isinstance(memory.get_handle(root=root), SequenceAccess)
    assert isinstance(memory.backend, SequenceStore)
