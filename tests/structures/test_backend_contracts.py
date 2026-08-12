"""Reusable contracts for backends supporting optional memory structures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import cast

import pytest

from genie import (
    AcyclicLinkStore,
    InMemoryBackend,
    KeyedStore,
    MemoryBackend,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    SequenceStore,
    SQLiteBackend,
    TemporalIndex,
    TemporalStore,
)
from genie.structures import (
    KeyBinding,
    MemoryAddress,
    TemporalEntry,
    ValidityInterval,
)


class StructureBackendContract:
    """Factory hook shared by optional backend capability contracts."""

    def make_backend(self) -> MemoryBackend:
        """Return a fresh backend under test."""

        raise NotImplementedError

    def put_blocks(
        self,
        backend: MemoryBackend,
        *block_ids: str,
    ) -> list[MemoryBlock]:
        """Create deterministic blocks required by a capability test."""

        return [
            backend.put_block(MemoryBlock(id=block_id, type="test", value=block_id))
            for block_id in block_ids
        ]


class BlockStoreContract(StructureBackendContract):
    """Contract tests for conditional core block mutations."""

    def test_block_update_compare_and_swap_contract(self) -> None:
        """A stale version cannot replace a block changed by another writer."""

        backend = self.make_backend()
        original = backend.put_block(MemoryBlock(id="mem_note", type="note", value={}))
        stale = backend.get_block(original.id)
        assert stale is not None

        backend.update_block(
            original.with_updates(value={"current": True}, version=2),
            expected_version=1,
        )
        with pytest.raises(MemoryConflict):
            backend.update_block(
                stale.with_updates(value={"stale": True}, version=2),
                expected_version=1,
            )

    def test_conditional_subtree_delete_contract(self) -> None:
        """Deletion rejects an incomplete or stale subtree snapshot."""

        backend = self.make_backend()
        root, child = self.put_blocks(backend, "mem_root", "mem_child")
        child = backend.update_block(child.with_updates(parent_id=root.id))

        with pytest.raises(MemoryConflict):
            backend.delete_block(root.id, expected_versions={root.id: root.version})

        assert backend.get_block(root.id) is not None
        assert backend.get_block(child.id) is not None


class KeyedStoreContract(StructureBackendContract):
    """Contract tests reusable by backends implementing ``KeyedStore``."""

    def test_keyed_store_contract(self) -> None:
        """Bindings are unique, ordered, conditional, and deletion-aware."""

        backend = self.make_backend()
        assert isinstance(backend, KeyedStore)
        store = cast(KeyedStore, backend)
        collection, first, second, third = self.put_blocks(
            backend,
            "mem_collection",
            "mem_first",
            "mem_second",
            "mem_third",
        )
        root_address = MemoryAddress((), "root")
        nested_a = MemoryAddress(("nested",), "a")
        nested_b = MemoryAddress(("nested",), "b")
        root_binding = KeyBinding(collection.id, root_address, first.id)

        assert store.bind_key(root_binding) == root_binding
        with pytest.raises(MemoryConflict):
            store.bind_key(root_binding)
        store.bind_key(KeyBinding(collection.id, nested_b, third.id))
        store.bind_key(KeyBinding(collection.id, nested_a, second.id))

        assert [binding.address for binding in store.list_keys(collection.id)] == [
            root_address,
            nested_a,
            nested_b,
        ]
        assert [
            binding.address
            for binding in store.list_keys(
                collection.id,
                namespace=("nested",),
                recursive=True,
                after=nested_a,
                limit=1,
            )
        ] == [nested_b]

        replacement = KeyBinding(collection.id, root_address, second.id)
        assert store.rebind_key(replacement, expected_block_id=first.id) == replacement
        with pytest.raises(MemoryConflict):
            store.rebind_key(root_binding, expected_block_id=first.id)
        with pytest.raises(MemoryConflict):
            store.unbind_key(
                collection.id,
                root_address,
                expected_block_id=first.id,
            )

        backend.delete_block(second.id)
        assert store.get_key(collection.id, root_address) is None
        backend.delete_block(collection.id)
        assert store.list_keys(collection.id) == []

    def test_keyed_store_bind_is_atomic(self) -> None:
        """Only one concurrent writer can claim an unused address."""

        backend = self.make_backend()
        assert isinstance(backend, KeyedStore)
        store = cast(KeyedStore, backend)
        collection = self.put_blocks(backend, "mem_collection")[0]
        targets = self.put_blocks(
            backend,
            *(f"mem_target_{index:02d}" for index in range(12)),
        )
        address = MemoryAddress((), "shared")
        barrier = Barrier(len(targets))

        def bind(target: MemoryBlock) -> bool:
            barrier.wait()
            try:
                store.bind_key(KeyBinding(collection.id, address, target.id))
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            outcomes = list(executor.map(bind, targets))

        assert outcomes.count(True) == 1
        assert store.get_key(collection.id, address) is not None


class SequenceStoreContract(StructureBackendContract):
    """Contract tests reusable by backends implementing ``SequenceStore``."""

    def test_sequence_store_contract(self) -> None:
        """Sequences are monotonic, range-readable, and deletion-aware."""

        backend = self.make_backend()
        assert isinstance(backend, SequenceStore)
        store = cast(SequenceStore, backend)
        log, first, second, third = self.put_blocks(
            backend,
            "mem_log",
            "mem_first",
            "mem_second",
            "mem_third",
        )

        assert store.append_log_entry(log.id, first.id).sequence == 0
        assert store.append_log_entry(log.id, second.id).sequence == 1
        assert store.append_log_entry(log.id, third.id).sequence == 2
        assert [
            entry.sequence
            for entry in store.list_log_entries(
                log.id,
                start=1,
                stop=3,
                reverse=True,
            )
        ] == [2, 1]

        backend.delete_block(second.id)
        assert store.get_log_entry(log.id, 1) is None
        replacement = self.put_blocks(backend, "mem_replacement")[0]
        assert store.append_log_entry(log.id, replacement.id).sequence == 3

        backend.delete_block(log.id)
        assert store.list_log_entries(log.id) == []

    def test_sequence_store_append_is_atomic(self) -> None:
        """Concurrent appends allocate unique positions in one log."""

        backend = self.make_backend()
        assert isinstance(backend, SequenceStore)
        store = cast(SequenceStore, backend)
        log, target = self.put_blocks(backend, "mem_log", "mem_target")

        with ThreadPoolExecutor(max_workers=12) as executor:
            entries = list(
                executor.map(
                    lambda _: store.append_log_entry(log.id, target.id),
                    range(100),
                )
            )

        assert sorted(entry.sequence for entry in entries) == list(range(100))


class TemporalStoreContract(StructureBackendContract):
    """Contract tests reusable by backends implementing ``TemporalStore``."""

    def test_temporal_store_contract(self) -> None:
        """Membership is unique, replaceable, ordered, and deletion-aware."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalStore)
        store = cast(TemporalStore, backend)
        collection, late, early = self.put_blocks(
            backend,
            "mem_collection",
            "mem_z",
            "mem_a",
        )
        early_entry = TemporalEntry(
            collection.id,
            early.id,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        late_entry = TemporalEntry(
            collection.id,
            late.id,
            validity=ValidityInterval(
                datetime(2026, 1, 2, tzinfo=UTC),
                datetime(2026, 1, 5, tzinfo=UTC),
            ),
        )

        assert store.add_temporal_entry(late_entry) == late_entry
        assert store.add_temporal_entry(early_entry) == early_entry
        with pytest.raises(MemoryConflict):
            store.add_temporal_entry(early_entry)
        assert [
            entry.block_id
            for entry in store.list_temporal_entries(collection.id, limit=1)
        ] == [early.id]
        assert [
            entry.block_id
            for entry in store.list_temporal_entries(
                collection.id,
                after_block_id=early.id,
            )
        ] == [late.id]

        replacement = TemporalEntry(
            collection.id,
            early.id,
            occurred_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        assert store.replace_temporal_entry(replacement) == replacement
        missing = TemporalEntry(
            collection.id,
            "mem_missing",
            occurred_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        with pytest.raises(MemoryConflict):
            store.replace_temporal_entry(missing)

        backend.delete_block(early.id)
        assert store.get_temporal_entry(collection.id, early.id) is None
        backend.delete_block(collection.id)
        assert store.list_temporal_entries(collection.id) == []

    def test_temporal_store_add_is_atomic(self) -> None:
        """One collection and block pair cannot be inserted twice."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalStore)
        store = cast(TemporalStore, backend)
        collection, target = self.put_blocks(
            backend,
            "mem_collection",
            "mem_target",
        )
        entry = TemporalEntry(
            collection.id,
            target.id,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        barrier = Barrier(12)

        def add(_: int) -> bool:
            barrier.wait()
            try:
                store.add_temporal_entry(entry)
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=12) as executor:
            outcomes = list(executor.map(add, range(12)))

        assert outcomes.count(True) == 1


class TemporalIndexContract(StructureBackendContract):
    """Contract tests reusable by backends implementing ``TemporalIndex``."""

    def test_temporal_index_contract(self) -> None:
        """Indexed filtering follows half-open time and stable paging rules."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalStore)
        assert isinstance(backend, TemporalIndex)
        store = cast(TemporalStore, backend)
        index = cast(TemporalIndex, backend)
        collection, event, valid, expired = self.put_blocks(
            backend,
            "mem_collection",
            "mem_event",
            "mem_valid",
            "mem_expired",
        )
        store.add_temporal_entry(
            TemporalEntry(
                collection.id,
                event.id,
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        store.add_temporal_entry(
            TemporalEntry(
                collection.id,
                valid.id,
                validity=ValidityInterval(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 5, tzinfo=UTC),
                ),
            )
        )
        store.add_temporal_entry(
            TemporalEntry(
                collection.id,
                expired.id,
                validity=ValidityInterval(
                    datetime(2025, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )
        )

        assert [
            entry.block_id
            for entry in index.query_temporal_entries(
                collection.id,
                occurred_only=True,
                occurred_start=datetime(2026, 1, 2, tzinfo=UTC),
                occurred_stop=datetime(2026, 1, 3, tzinfo=UTC),
            )
        ] == [event.id]
        assert [
            entry.block_id
            for entry in index.query_temporal_entries(
                collection.id,
                valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ] == [valid.id]


class AcyclicLinkStoreContract(StructureBackendContract):
    """Contract tests reusable by backends implementing ``AcyclicLinkStore``."""

    def test_acyclic_link_store_contract(self) -> None:
        """Protected relations reuse ordinary links and reject every cycle."""

        backend = self.make_backend()
        assert isinstance(backend, AcyclicLinkStore)
        store = cast(AcyclicLinkStore, backend)
        first, second, third = self.put_blocks(
            backend,
            "mem_first",
            "mem_second",
            "mem_third",
        )
        relation = "derived_into"
        existing = MemoryLink(first.id, second.id, relation)
        backend.add_link(existing)
        created = MemoryLink(second.id, third.id, relation)

        assert store.add_acyclic_link(created) == created
        with pytest.raises(MemoryConflict):
            store.add_acyclic_link(MemoryLink(third.id, first.id, relation))
        with pytest.raises(MemoryConflict):
            backend.add_link(MemoryLink(first.id, third.id, relation))
        assert backend.list_links(first.id, relation=relation) == [existing]

    def test_acyclic_link_check_and_insert_is_atomic(self) -> None:
        """Two opposite concurrent edges cannot both commit."""

        backend = self.make_backend()
        assert isinstance(backend, AcyclicLinkStore)
        store = cast(AcyclicLinkStore, backend)
        first, second = self.put_blocks(backend, "mem_first", "mem_second")
        barrier = Barrier(2)

        def add(link: MemoryLink) -> bool:
            barrier.wait()
            try:
                store.add_acyclic_link(link)
            except MemoryConflict:
                return False
            return True

        links = [
            MemoryLink(first.id, second.id, "derived_into"),
            MemoryLink(second.id, first.id, "derived_into"),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(add, links))

        assert outcomes.count(True) == 1


class TestInMemoryStructureBackendContracts(
    BlockStoreContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    AcyclicLinkStoreContract,
):
    """Run every optional structure contract against the reference backend."""

    def make_backend(self) -> InMemoryBackend:
        """Return an isolated in-memory backend."""

        return InMemoryBackend()


class TestSQLiteStructureBackendContracts(
    BlockStoreContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    AcyclicLinkStoreContract,
):
    """Run every optional structure contract against SQLite."""

    def setup_method(self) -> None:
        """Track in-memory SQLite connections created by one contract test."""

        self.backends: list[SQLiteBackend] = []

    def teardown_method(self) -> None:
        """Close every SQLite connection created by one contract test."""

        for backend in self.backends:
            backend.close()

    def make_backend(self) -> SQLiteBackend:
        """Return an isolated SQLite backend."""

        backend = SQLiteBackend()
        self.backends.append(backend)
        return backend
