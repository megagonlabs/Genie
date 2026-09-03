"""Reusable contracts for backends supporting optional memory structures."""

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

        current = backend.get_block(original.id)
        assert current is not None
        with pytest.raises(MemoryConflict):
            backend.update_block(
                current.with_updates(version=current.version + 2),
                expected_version=current.version,
            )

    def test_block_reference_integrity_contract(self) -> None:
        """Core writes reject duplicate identities and invalid parent references."""

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
        """Deletion rejects an incomplete or stale subtree snapshot."""

        backend = self.make_backend()
        root, child = self.put_blocks(backend, "mem_root", "mem_child")
        child = backend.update_block(child.with_updates(parent_id=root.id))

        with pytest.raises(MemoryConflict):
            backend.delete_block(root.id, expected_versions={root.id: root.version})

        assert backend.get_block(root.id) is not None
        assert backend.get_block(child.id) is not None


# Two separate trees, one of them three levels deep with a fork, so traversal
# has a shape to get wrong: a branch to move, siblings to keep apart, and a
# second root that owns none of the first one's blocks.
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
"""An id no test writes, so every method has to report it as absent."""


def _expected_children(tree: Mapping[str, str | None], parent_id: str) -> set[str]:
    """Return the ids whose recorded parent is `parent_id`."""

    return {block_id for block_id, parent in tree.items() if parent == parent_id}


def _expected_descendants(tree: Mapping[str, str | None], root_id: str) -> set[str]:
    """Return every id below `root_id`, by walking children downward."""

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
    """Contract tests reusable by backends implementing `TraversalIndex`.

    Ownership is recorded by `MemoryBlock.parent_id` in the block store, and
    `Memory` never reads that field: it asks these four methods instead. So an
    adapter that fills both roles has to answer them consistently with the
    parents it stored and consistently with each other.

    - **Relationships, never a strategy.** The shipped adapters answer with a
      recursive query, a graph path match, or a walk up the parent chain, and
      `children` is a different query from `contains` in every one of them.
      Nothing here prefers one.
    - **Order only where the protocol promises it.** `ancestors` documents
      nearest-parent-first order, so that is asserted. `children` and
      `descendants` are compared as sets, because their order is
      backend-defined.
    - **One adapter, not a composition.** A separately supplied
      `RoutedBackend(traversal_index=...)` is allowed to assert ownership the
      block store does not record. That freedom belongs to the composition, so
      it never reaches these tests.
    """

    def traversal_backend(self) -> MemoryBackend:
        """Return a backend holding `_TRAVERSAL_TREE`, both roles checked."""

        backend = self.make_backend()
        assert isinstance(backend, BlockStore)
        assert isinstance(backend, TraversalIndex)
        self.put_tree(backend, _TRAVERSAL_TREE)
        return backend

    def test_traversal_children_contract(self) -> None:
        """`children` returns exactly the blocks recording that block as parent."""

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

        This is the assertion that separates a wrong containment answer from a
        merely slow one, and it covers the two-tree case in passing.
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
        """`ancestors` walks nearest parent first and the chain holds together."""

        backend = self.traversal_backend()

        for block_id in _TRAVERSAL_TREE:
            returned = backend.ancestors(block_id)
            assert [block.id for block in returned] == _expected_ancestors(
                _TRAVERSAL_TREE, block_id
            )
            # Each returned block must record the next one as its own parent,
            # so the list is a real chain and not a set in some order.
            for position, ancestor in enumerate(returned):
                following = returned[position + 1].id if position + 1 < len(returned) else None
                assert ancestor.parent_id == following

        assert backend.ancestors(_MISSING_ID) == []

    def test_traversal_reparent_contract(self) -> None:
        """Moving a block moves the branch it owns in every traversal view."""

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
        """Deleting a block removes its whole subtree from every view."""

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
    """Contract tests reusable by backends implementing `KeyedStore`."""

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

    def test_keyed_store_rejects_missing_references_and_invalid_pages(self) -> None:
        """Keyed stores validate referential integrity and page arguments."""

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
        """Adapters can explicitly remove bindings for a deleted target or owner."""

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
    """Contract tests reusable by backends implementing `SequenceStore`."""

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

    def test_sequence_store_rejects_missing_references_and_invalid_ranges(self) -> None:
        """Sequence stores reject dangling appends and malformed range arguments."""

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
        """Adapters can explicitly remove entries for a target or entire log."""

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
    """Contract tests reusable by backends implementing `TemporalStore`."""

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

    def test_temporal_store_rejects_missing_references_and_invalid_pages(self) -> None:
        """Temporal stores validate references, removal state, and page limits."""

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
        """Adapters can explicitly remove memberships for a target or collection."""

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
    """Contract tests reusable by backends implementing `TemporalIndex`."""

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

    def test_temporal_index_rejects_invalid_query_arguments(self) -> None:
        """Indexed temporal queries reject malformed booleans, times, and limits."""

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
        """Overlap bounds and block-id cursors compose without boundary leaks."""

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
    """Contract tests reusable by backends implementing `LinkStore`."""

    def test_link_store_delete_block_cascade_contract(self) -> None:
        """Deleting a subtree removes every link that touches a removed block."""

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
        """The reserved mount relation is cleaned up from either endpoint."""

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
        """Adapters can remove one block's links without deleting the block."""

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
    """Contract tests reusable by backends implementing `AcyclicLinkStore`."""

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

    def test_acyclic_link_store_rejects_missing_endpoints(self) -> None:
        """Atomic DAG insertion preserves identity and endpoint integrity."""

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


class TestInMemoryStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
):
    """Run every optional structure contract against the reference backend."""

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
