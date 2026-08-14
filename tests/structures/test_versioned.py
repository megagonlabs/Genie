"""Tests for versioned records composed from keyed identities and logs."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from genie import (
    AccessDenied,
    InMemoryBackend,
    Memory,
    MemoryConflict,
    MemoryNotFound,
    UnsupportedOperation,
)
from genie.structures import (
    MemoryAddress,
    VersionedAccess,
    VersionedRecords,
    VersionedStore,
)


def test_versioned_records_keep_keyed_identity_and_positive_revisions() -> None:
    """A stable identity anchors positive ordered revision references."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="preferences")
    identity = memory.put(type="record_identity", value="favorite color")
    blue = memory.put(type="record_revision", value="blue")
    green = memory.put(type="record_revision", value="green")
    address = MemoryAddress(("users", "alice"), "favorite_color")
    records = VersionedRecords(memory, backend, collection=root)

    registered = records.register(address, identity)
    first = records.append(address, blue)
    second = records.append(address, green)

    assert registered.block == identity
    assert records.identity(address) == identity
    assert first.entry.record_id == identity.id
    assert first.entry.revision == 1
    assert first.block == blue
    assert second.entry.revision == 2
    assert records.current(address) == second
    assert records.revision(address, 1) == first
    assert records.history(address) == [first, second]
    assert records.identities()[0].binding.address == address
    assert identity.version == 1


def test_each_identity_has_an_independent_revision_sequence() -> None:
    """Logical revision numbering starts at one for every stable identity."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    first_address = MemoryAddress((), "first")
    second_address = MemoryAddress((), "second")
    first_identity = memory.put(type="record_identity", value="first")
    second_identity = memory.put(type="record_identity", value="second")
    first_revision = memory.put(type="revision", value="first value")
    second_revision = memory.put(type="revision", value="second value")
    records.register(first_address, first_identity)
    records.register(second_address, second_identity)

    assert records.append(first_address, first_revision).entry.revision == 1
    assert records.append(second_address, second_revision).entry.revision == 1


def test_record_registration_is_create_only() -> None:
    """The facade does not silently replace a stable keyed identity."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "record")
    first = memory.put(type="record_identity", value="first")
    second = memory.put(type="record_identity", value="second")
    records.register(address, first)

    with pytest.raises(MemoryConflict, match="already bound"):
        records.register(address, second)
    assert records.identity(address) == first


def test_revision_history_supports_ranges_reverse_order_and_limits() -> None:
    """History uses half-open positive revision ranges."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "record")
    identity = memory.put(type="record_identity", value="record")
    revisions = [memory.put(type="revision", value=index) for index in range(5)]
    records.register(address, identity)
    appended = [records.append(address, revision) for revision in revisions]

    assert records.history(address, start=2, stop=5) == appended[1:4]
    assert records.history(address, reverse=True, limit=2) == [
        appended[4],
        appended[3],
    ]
    assert records.history(address, start=3, stop=3) == []
    assert records.revision(address, 9) is None


def test_concurrent_appends_allocate_unique_record_revisions() -> None:
    """Concurrent writers receive one monotonic revision sequence per identity."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "record")
    identity = memory.put(type="record_identity", value="record")
    revisions = [memory.put(type="revision", value=index) for index in range(100)]
    records.register(address, identity)

    with ThreadPoolExecutor(max_workers=12) as executor:
        appended = list(executor.map(lambda block: records.append(address, block), revisions))

    assert sorted(item.entry.revision for item in appended) == list(range(1, 101))
    assert [item.entry.revision for item in records.history(address)] == list(
        range(1, 101)
    )


def test_versioned_records_preserve_handle_scope_for_history() -> None:
    """Scans omit hidden revisions while exact lookup and append propagate denial."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    identity = memory.put(parent=root, type="record_identity", value="record")
    inside = memory.put(parent=identity, type="revision", value="inside")
    outside = memory.put(type="revision", value="outside")
    address = MemoryAddress((), "record")
    full = VersionedRecords(memory, backend, collection=root)
    full.register(address, identity)
    first = full.append(address, inside)
    second = full.append(address, outside)
    scoped = VersionedRecords(
        memory.get_handle(root=root),
        backend,
        collection=root,
    )

    assert scoped.history(address) == [first]
    assert scoped.current(address) == first
    with pytest.raises(AccessDenied):
        scoped.revision(address, second.entry.revision)
    with pytest.raises(AccessDenied):
        scoped.append(address, outside)


def test_revision_deletion_leaves_a_gap_and_does_not_reuse_numbers() -> None:
    """Deleting a revision preserves later monotonic logical numbering."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "record")
    identity = memory.put(type="record_identity", value="record")
    first_block = memory.put(type="revision", value="first")
    second_block = memory.put(type="revision", value="second")
    third_block = memory.put(type="revision", value="third")
    records.register(address, identity)
    first = records.append(address, first_block)
    records.append(address, second_block)

    memory.delete(second_block)
    third = records.append(address, third_block)

    assert first.entry.revision == 1
    assert records.revision(address, 2) is None
    assert third.entry.revision == 3
    assert [item.entry.revision for item in records.history(address)] == [1, 3]


def test_identity_deletion_cleans_key_and_revision_log_state() -> None:
    """Deleting the stable identity removes its address and complete log."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "record")
    identity = memory.put(type="record_identity", value="record")
    revision = memory.put(type="revision", value="value")
    records.register(address, identity)
    records.append(address, revision)

    memory.delete(identity)

    assert records.identity(address) is None
    assert records.current(address) is None
    assert backend.list_log_entries(identity.id) == []
    with pytest.raises(MemoryNotFound, match="not registered"):
        records.append(address, revision)


def test_missing_address_has_empty_read_behavior() -> None:
    """Missing identities return empty reads while append reports the mistake."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "missing")
    revision = memory.put(type="revision", value="value")

    assert records.identity(address) is None
    assert records.current(address) is None
    assert records.revision(address, 1) is None
    assert records.history(address) == []
    with pytest.raises(MemoryNotFound, match="not registered"):
        records.append(address, revision)


@pytest.mark.parametrize(
    "operation",
    [
        lambda records, address: records.revision(address, 0),
        lambda records, address: records.revision(address, True),
        lambda records, address: records.history(address, start=0),
        lambda records, address: records.history(address, stop=0),
        lambda records, address: records.history(address, start=3, stop=2),
        lambda records, address: records.history(address, reverse=cast(bool, 1)),
        lambda records, address: records.history(address, limit=-1),
        lambda records, address: records.history(address, limit=True),
    ],
)
def test_versioned_records_reject_invalid_revision_ranges(
    operation: Callable[[VersionedRecords, MemoryAddress], object],
) -> None:
    """Revision ranges and limits use predictable validation errors."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="versioned_collection", value="records")
    records = VersionedRecords(memory, backend, collection=root)
    address = MemoryAddress((), "missing")

    with pytest.raises((TypeError, ValueError)):
        operation(records, address)


def test_versioned_records_requires_both_composed_store_capabilities() -> None:
    """A store missing keyed or sequence operations fails clearly."""

    memory = Memory()
    root = memory.put(type="versioned_collection", value="records")

    with pytest.raises(UnsupportedOperation, match="keyed versioned"):
        VersionedRecords(memory, object(), collection=root)  # type: ignore[arg-type]


def test_memory_handle_and_backend_implement_versioned_protocols() -> None:
    """Public access and the reference backend satisfy composition protocols."""

    memory = Memory()
    root = memory.put(type="versioned_collection", value="records")

    assert isinstance(memory, VersionedAccess)
    assert isinstance(memory.get_handle(root=root), VersionedAccess)
    assert isinstance(memory.backend, VersionedStore)
