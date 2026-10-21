"""Reusable contract tests for backends that support optional memory structures."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import cast

import pytest

from genie import (
    AcyclicLinkStore,
    InMemoryBackend,
    KeyedStore,
    LinkRelations,
    MemoryBackend,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    RouteCatalog,
    RouteInfo,
    SequenceStore,
    SQLiteBackend,
    TemporalIndex,
    TemporalStore,
)
from genie.backend.capabilities import BlockStore, LinkStore, TraversalIndex
from genie.structures import (
    KeyBinding,
    MemoryAddress,
    TemporalEntry,
    ValidityInterval,
)


class CoreOnlyBackend:
    """Core `MemoryBackend` that implements no optional capability.

    It forwards the core methods to an `InMemoryBackend`, and tests use it to
    check a backend that does not have a capability such as `AcyclicLinkStore`.
    """

    def __init__(self) -> None:
        """Wrap a new `InMemoryBackend`."""

        inner = InMemoryBackend()
        self.put_block = inner.put_block
        self.get_block = inner.get_block
        self.update_block = inner.update_block
        self.delete_block = inner.delete_block
        self.children = inner.children
        self.ancestors = inner.ancestors
        self.descendants = inner.descendants
        self.contains = inner.contains
        self.search = inner.search
        self.add_link = inner.add_link
        self.update_link = inner.update_link
        self.list_links = inner.list_links
        self.delete_links_for_block = inner.delete_links_for_block


class StructureBackendContract:
    """Factory hook that the contracts for optional backend capabilities share."""

    def make_backend(self) -> MemoryBackend:
        """Return a new instance of the backend under test."""

        raise NotImplementedError

    def put_blocks(
        self,
        backend: MemoryBackend,
        *block_ids: str,
    ) -> list[MemoryBlock]:
        """Create the blocks that a capability test needs, with fixed ids and values."""

        return [
            backend.put_block(MemoryBlock(id=block_id, type="test", value=block_id))
            for block_id in block_ids
        ]

    def put_tree(self, backend: MemoryBackend, tree: Mapping[str, str | None]) -> None:
        """Create blocks for an id-to-parent map, parents before children.

        Args:
            backend: Backend to write into.
            tree: Block id to parent block id, with `None` for a root.
        """

        written: set[str] = set()

        def write(block_id: str) -> None:
            if block_id in written:
                return
            parent_id = tree[block_id]
            if parent_id is not None:
                write(parent_id)
            backend.put_block(
                MemoryBlock(id=block_id, type="test", value=block_id, parent_id=parent_id)
            )
            written.add(block_id)

        for block_id in tree:
            write(block_id)


class BlockStoreContract(StructureBackendContract):
    """Contract tests for conditional writes and deletes of core blocks."""

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

        current = backend.get_block(original.id)
        assert current is not None
        with pytest.raises(MemoryConflict):
            backend.update_block(
                current.with_updates(version=current.version + 2),
                expected_version=current.version,
            )

    def test_block_reference_integrity_contract(self) -> None:
        """The backend rejects duplicate ids and invalid parent references on core writes."""

        backend = self.make_backend()
        existing = backend.put_block(
            MemoryBlock(id="mem_existing", type="note", value={})
        )

        with pytest.raises(MemoryConflict):
            backend.put_block(existing)
        with pytest.raises(MemoryConflict):
            backend.put_block(
                MemoryBlock(
                    id="mem_self_parent",
                    parent_id="mem_self_parent",
                    type="note",
                    value={},
                )
            )
        with pytest.raises(MemoryConflict):
            backend.put_block(
                MemoryBlock(
                    id="mem_orphan",
                    parent_id="mem_missing_parent",
                    type="note",
                    value={},
                )
            )
        with pytest.raises(MemoryConflict):
            backend.update_block(
                MemoryBlock(id="mem_missing", type="note", value={}, version=2)
            )

        assert backend.get_block("mem_orphan") is None
        assert backend.get_block("mem_missing") is None
        backend.delete_block("mem_missing")

    def test_conditional_subtree_delete_contract(self) -> None:
        """`delete_block` rejects an incomplete or stale subtree snapshot and deletes no block."""

        backend = self.make_backend()
        root, child = self.put_blocks(backend, "mem_root", "mem_child")
        child = backend.update_block(child.with_updates(parent_id=root.id))

        with pytest.raises(MemoryConflict):
            backend.delete_block(root.id, expected_versions={root.id: root.version})

        assert backend.get_block(root.id) is not None
        assert backend.get_block(child.id) is not None


# Two separate trees. One tree is 3 levels deep and has a fork. The shape gives
# an incorrect traversal 3 ways to fail. It has a branch to move, siblings to
# keep apart, and a second root that owns no block of the first tree.
_TRAVERSAL_TREE: Mapping[str, str | None] = {
    "mem_root": None,
    "mem_a": "mem_root",
    "mem_a1": "mem_a",
    "mem_a2": "mem_a",
    "mem_a2x": "mem_a2",
    "mem_b": "mem_root",
    "mem_b1": "mem_b",
    "mem_far": None,
    "mem_far1": "mem_far",
}

_MISSING_ID = "mem_never_stored"
"""An id that no test writes, so every traversal method reports it as absent."""


def _expected_children(tree: Mapping[str, str | None], parent_id: str) -> set[str]:
    """Return the ids whose recorded parent is `parent_id`."""

    return {block_id for block_id, parent in tree.items() if parent == parent_id}


def _expected_descendants(tree: Mapping[str, str | None], root_id: str) -> set[str]:
    """Return every id below `root_id` from a downward walk through the children."""

    found: set[str] = set()
    frontier = [root_id]
    while frontier:
        for child_id in _expected_children(tree, frontier.pop()):
            if child_id not in found:
                found.add(child_id)
                frontier.append(child_id)
    return found


def _expected_ancestors(tree: Mapping[str, str | None], block_id: str) -> list[str]:
    """Return the parent chain of `block_id`, nearest parent first."""

    chain: list[str] = []
    parent_id = tree.get(block_id)
    while parent_id is not None:
        chain.append(parent_id)
        parent_id = tree.get(parent_id)
    return chain


class TraversalIndexContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `TraversalIndex`.

    - **Ownership source.** The block store records ownership in
      `MemoryBlock.parent_id`. `Memory` never reads that field. It asks the
      four `TraversalIndex` methods instead. These tests check that an adapter
      that fills both roles gives answers that agree with the parents it
      stored and with each other.
    - **Relationships, never a strategy.** The shipped adapters use a
      recursive query, a graph path match, or a walk up the parent chain. In
      each adapter, `children` is a different query from `contains`. These
      tests do not prefer one method.
    - **Order only where the protocol promises it.** `ancestors` documents
      nearest-parent-first order, so the tests assert that order. The tests
      compare `children` and `descendants` as sets, because the backend
      defines their order.
    - **One adapter, not a composition.** A separate
      `RoutedBackend(traversal_index=...)` can assert ownership that the block
      store does not record. That freedom belongs to the composition, so these
      tests do not cover it.
    """

    def traversal_backend(self) -> MemoryBackend:
        """Return a backend that holds `_TRAVERSAL_TREE`, after a check that it is a `BlockStore` and a `TraversalIndex`."""

        backend = self.make_backend()
        assert isinstance(backend, BlockStore)
        assert isinstance(backend, TraversalIndex)
        self.put_tree(backend, _TRAVERSAL_TREE)
        return backend

    def test_traversal_children_contract(self) -> None:
        """`children` returns each block that records the given block as its parent exactly one time."""

        backend = self.traversal_backend()

        for block_id in _TRAVERSAL_TREE:
            returned = backend.children(block_id)
            ids = [block.id for block in returned]
            assert len(ids) == len(set(ids)), f"children({block_id!r}) repeated a block"
            assert set(ids) == _expected_children(_TRAVERSAL_TREE, block_id)
            for block in returned:
                assert block.parent_id == block_id

        assert backend.children(_MISSING_ID) == []

    def test_traversal_descendants_contract(self) -> None:
        """`descendants` is the transitive closure of `children`."""

        backend = self.traversal_backend()

        for block_id in _TRAVERSAL_TREE:
            expected = _expected_descendants(_TRAVERSAL_TREE, block_id)
            returned = backend.descendants(block_id)
            ids = [block.id for block in returned]
            assert len(ids) == len(set(ids)), f"descendants({block_id!r}) repeated a block"
            assert set(ids) == expected
            with_self = backend.descendants(block_id, include_self=True)
            assert {block.id for block in with_self} == expected | {block_id}

        assert backend.descendants(_MISSING_ID) == []

    def test_traversal_contains_contract(self) -> None:
        """`contains` agrees with `descendants` on every ordered pair.

        This assertion fails on a wrong containment answer but not on a slow
        one, and it also covers the case with two trees.
        """

        backend = self.traversal_backend()
        candidates = [*_TRAVERSAL_TREE, _MISSING_ID]

        for root_id in candidates:
            subtree = _expected_descendants(_TRAVERSAL_TREE, root_id)
            for block_id in candidates:
                expected = root_id in _TRAVERSAL_TREE and (
                    block_id == root_id or block_id in subtree
                )
                assert (
                    backend.contains(root_id, block_id) is expected
                ), f"contains({root_id!r}, {block_id!r})"

    def test_traversal_ancestors_contract(self) -> None:
        """`ancestors` returns the nearest parent first, and each block in the result records the next block as its parent."""

        backend = self.traversal_backend()

        for block_id in _TRAVERSAL_TREE:
            returned = backend.ancestors(block_id)
            assert [block.id for block in returned] == _expected_ancestors(
                _TRAVERSAL_TREE, block_id
            )
            # Each returned block records the next block as its parent. This check
            # proves that the list is a real chain, not only the correct set of blocks.
            for position, ancestor in enumerate(returned):
                following = returned[position + 1].id if position + 1 < len(returned) else None
                assert ancestor.parent_id == following

        assert backend.ancestors(_MISSING_ID) == []

    def test_traversal_reparent_contract(self) -> None:
        """A move of a block to a new parent also moves the branch that it owns in every traversal view."""

        backend = self.traversal_backend()
        moving = backend.get_block("mem_a2")
        assert moving is not None
        backend.update_block(
            moving.with_updates(parent_id="mem_b", version=moving.version + 1),
            expected_version=moving.version,
        )
        moved: Mapping[str, str | None] = {**_TRAVERSAL_TREE, "mem_a2": "mem_b"}

        for parent_id in ("mem_a", "mem_b"):
            assert {block.id for block in backend.children(parent_id)} == _expected_children(
                moved, parent_id
            )
        for root_id in ("mem_a", "mem_b", "mem_root"):
            assert {
                block.id for block in backend.descendants(root_id)
            } == _expected_descendants(moved, root_id)
        assert backend.contains("mem_b", "mem_a2x") is True
        assert backend.contains("mem_a", "mem_a2x") is False
        assert backend.contains("mem_root", "mem_a2x") is True
        assert [block.id for block in backend.ancestors("mem_a2x")] == _expected_ancestors(
            moved, "mem_a2x"
        )

    def test_traversal_delete_contract(self) -> None:
        """`delete_block` removes the whole subtree of the block from every traversal view."""

        backend = self.traversal_backend()
        removed = {"mem_a", "mem_a1", "mem_a2", "mem_a2x"}
        backend.delete_block("mem_a")
        remaining: Mapping[str, str | None] = {
            block_id: parent
            for block_id, parent in _TRAVERSAL_TREE.items()
            if block_id not in removed
        }

        assert {block.id for block in backend.children("mem_root")} == _expected_children(
            remaining, "mem_root"
        )
        assert {block.id for block in backend.descendants("mem_root")} == _expected_descendants(
            remaining, "mem_root"
        )
        for block_id in removed:
            assert backend.contains("mem_root", block_id) is False
            assert backend.ancestors(block_id) == []
            assert backend.children(block_id) == []
        assert backend.contains("mem_root", "mem_b1") is True


class KeyedStoreContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `KeyedStore`."""

    def test_keyed_store_contract(self) -> None:
        """`KeyedStore` keeps bindings unique and ordered, applies conditional rebinds and unbinds, and removes the bindings of a deleted target or collection."""

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
        store.unbind_key(
            collection.id,
            root_address,
            expected_block_id=second.id,
        )
        assert store.get_key(collection.id, root_address) is None
        store.bind_key(replacement)

        backend.delete_block(second.id)
        assert store.get_key(collection.id, root_address) is None
        backend.delete_block(collection.id)
        assert store.list_keys(collection.id) == []

    def test_keyed_store_bind_is_atomic(self) -> None:
        """Of 12 concurrent writers, only one can bind an unused address."""

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

    def test_keyed_store_rejects_missing_references_and_invalid_pages(self) -> None:
        """A `KeyedStore` rejects a `KeyBinding` that refers to a block that does not exist and rejects invalid page arguments."""

        backend = self.make_backend()
        assert isinstance(backend, KeyedStore)
        store = cast(KeyedStore, backend)
        collection, target = self.put_blocks(
            backend,
            "mem_collection",
            "mem_target",
        )
        address = MemoryAddress((), "key")

        with pytest.raises(MemoryConflict):
            store.bind_key(KeyBinding("mem_missing", address, target.id))
        with pytest.raises(MemoryConflict):
            store.bind_key(KeyBinding(collection.id, address, "mem_missing"))
        with pytest.raises(TypeError, match="recursive must be a boolean"):
            store.list_keys(collection.id, recursive=cast(bool, 1))
        with pytest.raises(TypeError, match="limit must be an integer"):
            store.list_keys(collection.id, limit=cast(int, True))
        with pytest.raises(ValueError, match="limit must be greater"):
            store.list_keys(collection.id, limit=-1)

    def test_keyed_store_explicit_cleanup_capability(self) -> None:
        """An adapter can remove the bindings of one target or one owner collection explicitly, with `delete_key_bindings_for_block`."""

        backend = self.make_backend()
        assert isinstance(backend, KeyedStore)
        store = cast(KeyedStore, backend)
        collection, first, second = self.put_blocks(
            backend,
            "mem_collection",
            "mem_first",
            "mem_second",
        )
        first_address = MemoryAddress((), "first")
        second_address = MemoryAddress((), "second")
        store.bind_key(KeyBinding(collection.id, first_address, first.id))
        store.bind_key(KeyBinding(collection.id, second_address, second.id))

        store.delete_key_bindings_for_block(first.id)
        assert store.get_key(collection.id, first_address) is None
        assert store.get_key(collection.id, second_address) is not None
        store.delete_key_bindings_for_block(collection.id)
        assert store.list_keys(collection.id) == []


class SequenceStoreContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `SequenceStore`."""

    def test_sequence_store_contract(self) -> None:
        """`SequenceStore` gives each new entry a higher position, never reuses a position, reads ranges, and removes the entries of a deleted block or log."""

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
        """Concurrent appends to one log get unique positions."""

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

    def test_sequence_store_rejects_missing_references_and_invalid_ranges(self) -> None:
        """A `SequenceStore` rejects an append with a log or target that does not exist, and it rejects malformed range arguments."""

        backend = self.make_backend()
        assert isinstance(backend, SequenceStore)
        store = cast(SequenceStore, backend)
        log, target = self.put_blocks(backend, "mem_log", "mem_target")

        with pytest.raises(MemoryConflict):
            store.append_log_entry("mem_missing", target.id)
        with pytest.raises(MemoryConflict):
            store.append_log_entry(log.id, "mem_missing")
        with pytest.raises(TypeError, match="sequence must be an integer"):
            store.get_log_entry(log.id, cast(int, True))
        with pytest.raises(ValueError, match="sequence must be greater"):
            store.get_log_entry(log.id, -1)
        with pytest.raises(TypeError, match="reverse must be a boolean"):
            store.list_log_entries(log.id, reverse=cast(bool, 1))
        with pytest.raises(ValueError, match="stop must be greater"):
            store.list_log_entries(log.id, start=2, stop=1)
        with pytest.raises(TypeError, match="limit must be an integer"):
            store.list_log_entries(log.id, limit=cast(int, True))
        with pytest.raises(ValueError, match="limit must be greater"):
            store.list_log_entries(log.id, limit=-1)

    def test_sequence_store_explicit_cleanup_capability(self) -> None:
        """An adapter can remove the entries of one target or of a whole log explicitly, with `delete_log_entries_for_block`."""

        backend = self.make_backend()
        assert isinstance(backend, SequenceStore)
        store = cast(SequenceStore, backend)
        log, first, second = self.put_blocks(
            backend,
            "mem_log",
            "mem_first",
            "mem_second",
        )
        store.append_log_entry(log.id, first.id)
        store.append_log_entry(log.id, second.id)

        store.delete_log_entries_for_block(first.id)
        assert [entry.block_id for entry in store.list_log_entries(log.id)] == [
            second.id
        ]
        store.delete_log_entries_for_block(log.id)
        assert store.list_log_entries(log.id) == []


class TemporalStoreContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `TemporalStore`."""

    def test_temporal_store_contract(self) -> None:
        """`TemporalStore` keeps memberships unique and ordered, replaces only an entry that exists, and removes the entries of a deleted block or collection."""

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
        """Of 12 concurrent writers, only one can add the same collection and block pair."""

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

    def test_temporal_store_rejects_missing_references_and_invalid_pages(self) -> None:
        """A `TemporalStore` rejects references to blocks that do not exist, a replace or a remove of an absent entry, and invalid page limits."""

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

        with pytest.raises(MemoryConflict):
            store.add_temporal_entry(
                TemporalEntry(
                    "mem_missing",
                    target.id,
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        with pytest.raises(MemoryConflict):
            store.add_temporal_entry(
                TemporalEntry(
                    collection.id,
                    "mem_missing",
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        with pytest.raises(MemoryConflict):
            store.replace_temporal_entry(entry)
        store.add_temporal_entry(entry)
        store.remove_temporal_entry(collection.id, target.id)
        assert store.get_temporal_entry(collection.id, target.id) is None
        with pytest.raises(MemoryConflict):
            store.remove_temporal_entry(collection.id, target.id)
        with pytest.raises(TypeError, match="limit must be an integer"):
            store.list_temporal_entries(collection.id, limit=cast(int, True))
        with pytest.raises(ValueError, match="limit must be greater"):
            store.list_temporal_entries(collection.id, limit=-1)

    def test_temporal_store_explicit_cleanup_capability(self) -> None:
        """An adapter can remove the memberships of one target or of one collection explicitly, with `delete_temporal_entries_for_block`."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalStore)
        store = cast(TemporalStore, backend)
        collection, first, second = self.put_blocks(
            backend,
            "mem_collection",
            "mem_first",
            "mem_second",
        )
        for target in (first, second):
            store.add_temporal_entry(
                TemporalEntry(
                    collection.id,
                    target.id,
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )

        store.delete_temporal_entries_for_block(first.id)
        assert [
            entry.block_id for entry in store.list_temporal_entries(collection.id)
        ] == [second.id]
        store.delete_temporal_entries_for_block(collection.id)
        assert store.list_temporal_entries(collection.id) == []


class TemporalIndexContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `TemporalIndex`."""

    def test_temporal_index_contract(self) -> None:
        """`query_temporal_entries` uses half-open time ranges and a stable page order."""

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

    def test_temporal_index_rejects_invalid_query_arguments(self) -> None:
        """`query_temporal_entries` rejects malformed booleans, times, intervals, and limits."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalIndex)
        index = cast(TemporalIndex, backend)

        with pytest.raises(TypeError, match="occurred_only must be a boolean"):
            index.query_temporal_entries("collection", occurred_only=cast(bool, 1))
        with pytest.raises(TypeError, match="occurred_start must be a datetime"):
            index.query_temporal_entries(
                "collection",
                occurred_start=cast(datetime, "bad"),
            )
        with pytest.raises(ValueError, match="occurred_start must be timezone-aware"):
            index.query_temporal_entries(
                "collection",
                occurred_start=datetime(2026, 1, 1),  # noqa: DTZ001 - invalid input
            )
        with pytest.raises(ValueError, match="occurred_stop must be greater"):
            index.query_temporal_entries(
                "collection",
                occurred_start=datetime(2026, 1, 2, tzinfo=UTC),
                occurred_stop=datetime(2026, 1, 1, tzinfo=UTC),
            )
        with pytest.raises(TypeError, match="overlaps must be a ValidityInterval"):
            index.query_temporal_entries(
                "collection",
                overlaps=cast(ValidityInterval, object()),
            )
        with pytest.raises(TypeError, match="limit must be an integer"):
            index.query_temporal_entries("collection", limit=cast(int, True))
        with pytest.raises(ValueError, match="limit must be greater"):
            index.query_temporal_entries("collection", limit=-1)

    def test_temporal_index_overlap_and_paging_contract(self) -> None:
        """Overlap bounds and block-id cursors work together, and no entry outside a bound appears in the result."""

        backend = self.make_backend()
        assert isinstance(backend, TemporalStore)
        assert isinstance(backend, TemporalIndex)
        store = cast(TemporalStore, backend)
        index = cast(TemporalIndex, backend)
        collection, first, second, third = self.put_blocks(
            backend,
            "mem_collection",
            "mem_a",
            "mem_b",
            "mem_c",
        )
        for target, start, stop in (
            (
                first,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 3, tzinfo=UTC),
            ),
            (
                second,
                datetime(2026, 1, 2, tzinfo=UTC),
                None,
            ),
            (
                third,
                datetime(2026, 1, 4, tzinfo=UTC),
                datetime(2026, 1, 5, tzinfo=UTC),
            ),
        ):
            store.add_temporal_entry(
                TemporalEntry(
                    collection.id,
                    target.id,
                    validity=ValidityInterval(start, stop),
                )
            )

        assert [
            entry.block_id
            for entry in index.query_temporal_entries(
                collection.id,
                overlaps=ValidityInterval(
                    datetime(2026, 1, 2, tzinfo=UTC),
                    datetime(2026, 1, 4, tzinfo=UTC),
                ),
                after_block_id=first.id,
                limit=1,
            )
        ] == [second.id]
        assert [
            entry.block_id
            for entry in index.query_temporal_entries(
                collection.id,
                overlaps=ValidityInterval(datetime(2026, 1, 5, tzinfo=UTC)),
            )
        ] == [second.id]


class LinkStoreContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `LinkStore`."""

    def test_link_store_delete_block_cascade_contract(self) -> None:
        """`delete_block` on a subtree removes every link that touches a deleted block."""

        backend = self.make_backend()
        assert isinstance(backend, LinkStore)
        root, branch, leaf, outside = self.put_blocks(
            backend,
            "mem_root",
            "mem_branch",
            "mem_leaf",
            "mem_outside",
        )
        branch = backend.update_block(branch.with_updates(parent_id=root.id))
        leaf = backend.update_block(leaf.with_updates(parent_id=branch.id))
        internal = MemoryLink(root.id, leaf.id, "contains")
        inbound = MemoryLink(outside.id, branch.id, "references")
        outbound = MemoryLink(leaf.id, outside.id, "cites")
        for link in (internal, inbound, outbound):
            backend.add_link(link)
        assert len(backend.list_links(outside.id, direction="both")) == 2

        backend.delete_block(root.id)

        assert backend.get_block(outside.id) is not None
        for removed in (root, branch, leaf):
            assert backend.get_block(removed.id) is None
            assert backend.list_links(removed.id, direction="both") == []
        assert backend.list_links(outside.id, direction="both") == []

    def test_link_store_delete_block_cascade_covers_mounts(self) -> None:
        """`delete_block` removes links with the reserved mount relation at either endpoint."""

        backend = self.make_backend()
        assert isinstance(backend, LinkStore)
        root, leaf, other = self.put_blocks(
            backend,
            "mem_root",
            "mem_leaf",
            "mem_other",
        )
        leaf = backend.update_block(leaf.with_updates(parent_id=root.id))
        backend.add_link(MemoryLink(leaf.id, other.id, LinkRelations.MOUNT))
        backend.add_link(MemoryLink(other.id, leaf.id, LinkRelations.MOUNT))
        assert len(backend.list_links(other.id, direction="both")) == 2

        backend.delete_block(root.id)

        assert backend.get_block(other.id) is not None
        assert backend.list_links(other.id, relation=LinkRelations.MOUNT, direction="both") == []
        assert backend.list_links(other.id, direction="both") == []

    def test_link_store_explicit_cleanup_capability(self) -> None:
        """`delete_links_for_block` removes the links of one block and keeps the block."""

        backend = self.make_backend()
        assert isinstance(backend, LinkStore)
        store = cast(LinkStore, backend)
        source, target, other = self.put_blocks(
            backend,
            "mem_source",
            "mem_target",
            "mem_other",
        )
        backend.add_link(MemoryLink(source.id, target.id, "cites"))
        backend.add_link(MemoryLink(other.id, source.id, "cites"))
        kept = MemoryLink(other.id, target.id, "cites")
        backend.add_link(kept)

        store.delete_links_for_block(source.id)

        assert backend.get_block(source.id) is not None
        assert backend.list_links(source.id, direction="both") == []
        assert backend.list_links(target.id, direction="both") == [kept]
        store.delete_links_for_block("mem_missing")
        assert backend.list_links(other.id, direction="out") == [kept]


class AcyclicLinkStoreContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `AcyclicLinkStore`."""

    def test_acyclic_link_store_contract(self) -> None:
        """A protected relation reuses the ordinary links it already has, rejects every cycle, and rejects a later plain `add_link`."""

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

    def test_acyclic_link_store_rejects_missing_endpoints(self) -> None:
        """`add_acyclic_link` rejects an endpoint that does not exist and a duplicate link id."""

        backend = self.make_backend()
        assert isinstance(backend, AcyclicLinkStore)
        store = cast(AcyclicLinkStore, backend)
        source, target = self.put_blocks(backend, "mem_source", "mem_target")

        with pytest.raises(MemoryConflict):
            store.add_acyclic_link(
                MemoryLink("mem_missing", target.id, "derived_into")
            )
        with pytest.raises(MemoryConflict):
            store.add_acyclic_link(
                MemoryLink(source.id, "mem_missing", "derived_into")
            )
        stored = MemoryLink(
            source.id,
            target.id,
            "derived_into",
            id="mem_link",
        )
        assert store.add_acyclic_link(stored) == stored
        with pytest.raises(MemoryConflict):
            store.add_acyclic_link(
                MemoryLink(source.id, target.id, "derived_into", id=stored.id)
            )


class RouteCatalogContract(StructureBackendContract):
    """Reusable contract tests for backends that implement `RouteCatalog`."""

    def test_route_catalog_contract(self) -> None:
        """`RouteCatalog` keeps one entry for each block id, rejects a second claim, and ignores the release of an id with no entry."""

        backend = self.make_backend()
        assert isinstance(backend, RouteCatalog)
        catalog = cast(RouteCatalog, backend)
        entry = RouteInfo("mem_block", "us")
        other = RouteInfo("mem_other", "eu")

        assert catalog.get_route(entry.block_id) is None
        assert catalog.claim_route(entry) == entry
        assert catalog.get_route(entry.block_id) == entry
        with pytest.raises(MemoryConflict):
            catalog.claim_route(RouteInfo(entry.block_id, "eu"))
        with pytest.raises(MemoryConflict):
            catalog.claim_route(entry)
        assert catalog.get_route(entry.block_id) == entry

        catalog.claim_route(other)
        catalog.release_routes([entry.block_id, "mem_never_claimed"])
        assert catalog.get_route(entry.block_id) is None
        assert catalog.get_route(other.block_id) == other
        moved = RouteInfo(entry.block_id, "eu")
        assert catalog.claim_route(moved) == moved
        assert catalog.get_route(entry.block_id) == moved
        catalog.release_routes([])
        catalog.release_routes([entry.block_id, other.block_id])
        assert catalog.get_route(entry.block_id) is None
        assert catalog.get_route(other.block_id) is None

    def test_route_catalog_claim_is_atomic(self) -> None:
        """Of 12 concurrent claims for one block id, each with its own route, only one succeeds, and its route is stored."""

        backend = self.make_backend()
        assert isinstance(backend, RouteCatalog)
        catalog = cast(RouteCatalog, backend)
        entries = [RouteInfo("mem_shared", f"route_{index:02d}") for index in range(12)]
        barrier = Barrier(len(entries))

        def claim(entry: RouteInfo) -> bool:
            barrier.wait()
            try:
                catalog.claim_route(entry)
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=len(entries)) as executor:
            outcomes = list(executor.map(claim, entries))

        assert outcomes.count(True) == 1
        assert catalog.get_route("mem_shared") == entries[outcomes.index(True)]

    def test_route_catalog_entries_are_separate_from_blocks(self) -> None:
        """An entry can use the id of a stored block or of no block, and block writes and deletes leave the entries unchanged."""

        backend = self.make_backend()
        assert isinstance(backend, RouteCatalog)
        catalog = cast(RouteCatalog, backend)
        block = self.put_blocks(backend, "mem_block")[0]
        stored_entry = RouteInfo(block.id, "us")
        pending_entry = RouteInfo("mem_pending", "eu")

        catalog.claim_route(stored_entry)
        catalog.claim_route(pending_entry)
        assert backend.get_block(block.id) == block
        assert backend.get_block(pending_entry.block_id) is None

        pending_block = backend.put_block(MemoryBlock(id=pending_entry.block_id, type="test", value="pending"))
        backend.delete_block(block.id)
        assert catalog.get_route(stored_entry.block_id) == stored_entry
        assert catalog.get_route(pending_entry.block_id) == pending_entry

        catalog.release_routes([stored_entry.block_id, pending_entry.block_id])
        assert backend.get_block(pending_entry.block_id) == pending_block


class TestInMemoryStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
    RouteCatalogContract,
):
    """Run every contract in this module against `InMemoryBackend`, the reference backend."""

    def make_backend(self) -> InMemoryBackend:
        """Return an isolated in-memory backend."""

        return InMemoryBackend()


class TestSQLiteStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
    RouteCatalogContract,
):
    """Run every contract in this module against `SQLiteBackend`."""

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
