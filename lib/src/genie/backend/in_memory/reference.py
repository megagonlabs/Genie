"""In-memory reference backend.

- **Small on purpose:** the backend has no third-party dependencies.
- **Use:** tests, examples, and local prototypes.
- **Reference:** it is also executable documentation for the backend
  interfaces.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Literal

from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict
from genie.memory.links import MemoryLink
from genie.query.filters import matches_filters
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult
from genie.query.scope import Scope

if TYPE_CHECKING:
    from genie.backend.composite.routed import RouteInfo
    from genie.structures.records import (
        KeyBinding,
        LogEntry,
        MemoryAddress,
        TemporalEntry,
        ValidityInterval,
    )


class InMemoryBackend:
    """Single-process backend that implements all Genie backend interfaces."""

    def __init__(self) -> None:
        """Create an empty in-memory backend."""

        self._blocks: dict[str, MemoryBlock] = {}
        self._children: dict[str, list[str]] = defaultdict(list)
        self._links: dict[str, MemoryLink] = {}
        self._acyclic_relations: set[str] = set()
        self._key_bindings: dict[
            tuple[str, tuple[str, ...], str],
            KeyBinding,
        ] = {}
        self._log_entries: dict[str, dict[int, LogEntry]] = defaultdict(dict)
        self._next_log_sequence: dict[str, int] = {}
        self._temporal_entries: dict[tuple[str, str], TemporalEntry] = {}
        self._route_entries: dict[str, RouteInfo] = {}
        self._structure_lock = RLock()

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new encoded block.

        Args:
            block: Block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists or the parent is missing.
        """

        with self._structure_lock:
            if block.id in self._blocks:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            if block.parent_id is not None and block.parent_id not in self._blocks:
                raise MemoryConflict(f"Parent memory block {block.parent_id!r} does not exist.")
            block = _copy_block(block)
            self._blocks[block.id] = block
            if block.parent_id is not None:
                self._children[block.parent_id].append(block.id)
            return block

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Get a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            Defensive copy of the block, or `None` when it does not exist.
        """

        with self._structure_lock:
            block = self._blocks.get(block_id)
            return _copy_block(block) if block is not None else None

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace an existing encoded block, with an optional version check.

        Args:
            block: Replacement block.
            expected_version: Expected current version for compare-and-swap.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block does not exist, its version changed,
                the replacement version is not `expected_version + 1`, the
                parent does not exist, or the change makes a parent cycle.
        """

        with self._structure_lock:
            previous = self._blocks.get(block.id)
            if previous is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._move_parent(previous, block)
            block = _copy_block(block)
            self._blocks[block.id] = block
            return _copy_block(block)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a block and its descendants, with an optional exact snapshot.

        Args:
            block_id: Block id to delete. An unknown id is ignored, unless
                `expected_versions` is not empty.
            expected_versions: Exact subtree id-to-version snapshot for the
                delete.

        Raises:
            MemoryConflict: If the given subtree snapshot is stale. For an
                unknown id, a snapshot that is not empty is stale.
        """

        with self._structure_lock:
            if expected_versions is not None:
                current = {block.id: block.version for block in self.descendants(block_id, include_self=True)}
                if current != dict(expected_versions):
                    raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            self._delete_block_unchecked(block_id)

    def _delete_block_unchecked(self, block_id: str) -> None:
        """Delete one subtree while this method holds the structure lock."""

        block = self._blocks.get(block_id)
        if block is None:
            return
        for child in list(self._children.get(block_id, ())):
            self._delete_block_unchecked(child)
        if block.parent_id is not None and block_id in self._children.get(block.parent_id, []):
            self._children[block.parent_id].remove(block_id)
        self._children.pop(block_id, None)
        self._blocks.pop(block_id, None)
        self.delete_links_for_block(block_id)
        self.delete_key_bindings_for_block(block_id)
        self.delete_log_entries_for_block(block_id)
        self.delete_temporal_entries_for_block(block_id)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return the direct child blocks of a parent id.

        Args:
            block_id: Parent block id.

        Returns:
            Defensive copies of direct child blocks.
        """

        with self._structure_lock:
            return [_copy_block(self._blocks[child_id]) for child_id in self._children.get(block_id, ())]

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return the ancestors, from the nearest parent upward.

        Args:
            block_id: Block id to examine.

        Returns:
            Defensive copies of ancestor blocks.
        """

        with self._structure_lock:
            ancestors: list[MemoryBlock] = []
            block = self._blocks.get(block_id)
            while block is not None and block.parent_id is not None:
                parent = self._blocks.get(block.parent_id)
                if parent is None:
                    break
                ancestors.append(_copy_block(parent))
                block = parent
            return ancestors

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return descendants in depth-first order.

        Args:
            block_id: Root block id.
            include_self: If `True`, the result includes `block_id`.

        Returns:
            Defensive copies of descendant blocks.
        """

        with self._structure_lock:
            blocks: list[MemoryBlock] = []
            if include_self and block_id in self._blocks:
                blocks.append(_copy_block(self._blocks[block_id]))
            for child_id in self._children.get(block_id, ()):
                blocks.extend(self.descendants(child_id, include_self=True))
            return blocks

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is in the subtree of `root_id`.

        - **Walk up:** the check walks up from the candidate. It reads one block
          for each level, and it does not build the whole subtree. So the cost
          grows with the tree depth, not with the subtree size.
        - **Other adapters:** `MongoDBBackend` and `RedisBackend` also walk up.
          `SQLiteBackend` and `PostgresBackend` walk down from the root in one
          recursive query, and they stop at the first hit.
        - **Why it matters:** `Memory` calls this once for each result of a
          `subtree` or `ancestors` search, and `MemoryHandle` calls it on each
          bounded read.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when `block_id` is `root_id` or is below it.
        """

        with self._structure_lock:
            if root_id not in self._blocks:
                return False
            if root_id == block_id:
                return True
            block = self._blocks.get(block_id)
            seen = {block_id}
            while block is not None and block.parent_id is not None:
                if block.parent_id == root_id:
                    return True
                if block.parent_id in seen:
                    # This line does not run while the no-cycle invariant
                    # holds. `_move_parent` enforces that invariant with this
                    # same method. The guard stops a damaged index from an
                    # infinite loop.
                    return False
                seen.add(block.parent_id)
                block = self._blocks.get(block.parent_id)
            return False

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Bind an unused collection address to a block, in one atomic step.

        Args:
            binding: Key binding to create.

        Returns:
            Stored binding.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the address is already bound.
        """

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            slot = _key_slot(binding.collection_id, binding.address)
            if slot in self._key_bindings:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in " f"collection {binding.collection_id!r}.")
            self._key_bindings[slot] = binding
            return binding

    def rebind_key(
        self,
        binding: KeyBinding,
        *,
        expected_block_id: str,
    ) -> KeyBinding:
        """Replace a key binding in one atomic compare-and-swap step.

        Args:
            binding: Replacement binding. Its collection id and address select
                the existing binding to replace.
            expected_block_id: Block id that the binding points to now.

        Returns:
            Stored replacement binding.

        Raises:
            MemoryConflict: If the collection or the block does not exist, the
                address is not bound, or it no longer points to
                `expected_block_id`.
        """

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            slot = _key_slot(binding.collection_id, binding.address)
            current = self._key_bindings.get(slot)
            if current is None or current.block_id != expected_block_id:
                raise MemoryConflict(f"Key address {binding.address!r} in collection " f"{binding.collection_id!r} no longer points to expected " f"block {expected_block_id!r}.")
            self._key_bindings[slot] = binding
            return binding

    def get_key(
        self,
        collection_id: str,
        address: MemoryAddress,
    ) -> KeyBinding | None:
        """Return one exact key binding.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to look up.

        Returns:
            Stored binding, or `None` when the address is unused.
        """

        with self._structure_lock:
            return self._key_bindings.get(_key_slot(collection_id, address))

    def list_keys(
        self,
        collection_id: str,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        after: MemoryAddress | None = None,
        limit: int = 100,
    ) -> list[KeyBinding]:
        """Return one bounded page in stable namespace-and-key order.

        Args:
            collection_id: Id of the collection to list.
            namespace: Optional exact namespace filter. With `recursive=True`,
                it is a namespace prefix.
            recursive: If `True`, the result also includes the nested
                namespaces below `namespace`.
            after: Optional exclusive start address, for pages in address
                order.
            limit: Maximum number of bindings to return.

        Returns:
            Matching bindings in ascending namespace-and-key order.

        Raises:
            TypeError: If `recursive` is not a boolean or `limit` is not an
                integer.
            ValueError: If `limit` is negative.
        """

        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a boolean.")
        _validate_limit(limit)
        with self._structure_lock:
            bindings = [
                binding
                for binding in self._key_bindings.values()
                if binding.collection_id == collection_id
                and _namespace_matches(
                    binding.address.namespace,
                    namespace,
                    recursive=recursive,
                )
            ]
            bindings = sorted(
                bindings,
                key=lambda binding: (binding.address.namespace, binding.address.key),
            )
            if after is not None:
                after_key = (after.namespace, after.key)
                bindings = [binding for binding in bindings if (binding.address.namespace, binding.address.key) > after_key]
            return bindings[:limit]

    def unbind_key(
        self,
        collection_id: str,
        address: MemoryAddress,
        *,
        expected_block_id: str,
    ) -> None:
        """Remove a key binding in one atomic step, if its block id still matches.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to remove.
            expected_block_id: Block id that the binding points to now.

        Raises:
            MemoryConflict: If the address is not bound, or it no longer points
                to `expected_block_id`.
        """

        with self._structure_lock:
            slot = _key_slot(collection_id, address)
            current = self._key_bindings.get(slot)
            if current is None or current.block_id != expected_block_id:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} " f"no longer points to expected block {expected_block_id!r}.")
            self._key_bindings.pop(slot)

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove the bindings that one deleted block owns, or that point to it.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        with self._structure_lock:
            for slot, binding in list(self._key_bindings.items()):
                if binding.collection_id == block_id or binding.block_id == block_id:
                    self._key_bindings.pop(slot, None)

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Append a block reference with a unique sequence number, in one atomic step.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry with the allocated sequence number.

        Raises:
            MemoryConflict: If the log or the referenced block does not exist.
        """

        from genie.structures.records import LogEntry

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=log_id,
                block_id=block_id,
                container_name="Event log",
            )
            sequence = self._next_log_sequence.get(log_id, 0)
            entry = LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)
            self._log_entries[log_id][sequence] = entry
            self._next_log_sequence[log_id] = sequence + 1
            return entry

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry.

        Args:
            log_id: Id of the ordered log.
            sequence: Exact position in the log.

        Returns:
            Stored entry, or `None` when the position has no entry.

        Raises:
            TypeError: If `sequence` is not an integer.
            ValueError: If `sequence` is negative.
        """

        _validate_sequence(sequence, name="sequence")
        with self._structure_lock:
            return self._log_entries.get(log_id, {}).get(sequence)

    def list_log_entries(
        self,
        log_id: str,
        *,
        start: int | None = None,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Return one bounded page in a half-open sequence range.

        Args:
            log_id: Id of the ordered log.
            start: Optional inclusive lower sequence bound.
            stop: Optional exclusive upper sequence bound.
            reverse: If `True`, the entries come in descending sequence order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in sequence order, descending when `reverse` is
            `True`.

        Raises:
            TypeError: If `reverse` is not a boolean, or `start`, `stop`, or
                `limit` is not an integer.
            ValueError: If a bound or `limit` is negative, or `stop` is less
                than `start`.
        """

        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean.")
        if start is not None:
            _validate_sequence(start, name="start")
        if stop is not None:
            _validate_sequence(stop, name="stop")
        if start is not None and stop is not None and stop < start:
            raise ValueError("stop must be greater than or equal to start.")
        _validate_limit(limit)
        with self._structure_lock:
            entries = [entry for sequence, entry in self._log_entries.get(log_id, {}).items() if (start is None or sequence >= start) and (stop is None or sequence < stop)]
            entries = sorted(
                entries,
                key=lambda entry: entry.sequence,
                reverse=reverse,
            )
            return entries[:limit]

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove the log that one deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. The backend drops the log that
                this id owns, with its sequence counter. It also drops the
                entries that point to this id in all other logs.
        """

        with self._structure_lock:
            self._log_entries.pop(block_id, None)
            self._next_log_sequence.pop(block_id, None)
            for log_id, entries in list(self._log_entries.items()):
                for sequence, entry in list(entries.items()):
                    if entry.block_id == block_id:
                        entries.pop(sequence, None)
                if not entries:
                    self._log_entries.pop(log_id, None)

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Add unique temporal membership for one block, in one atomic step.

        Args:
            entry: Collection id, block id, and event time or validity period
                to store.

        Returns:
            Stored entry.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the collection already contains the block.
        """

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            slot = (entry.collection_id, entry.block_id)
            if slot in self._temporal_entries:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal " f"collection {entry.collection_id!r}.")
            self._temporal_entries[slot] = entry
            return entry

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Replace temporal membership for one existing block, in one atomic step.

        Args:
            entry: Replacement entry. Its collection id and block id select
                the existing entry to replace.

        Returns:
            Stored replacement entry.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the collection does not contain the block.
        """

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            slot = (entry.collection_id, entry.block_id)
            if slot not in self._temporal_entries:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal " f"collection {entry.collection_id!r}.")
            self._temporal_entries[slot] = entry
            return entry

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Return one exact temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block.

        Returns:
            Stored entry, or `None` when the collection does not contain the
            block.
        """

        with self._structure_lock:
            return self._temporal_entries.get((collection_id, block_id))

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Return one temporal membership page in stable block-id order.

        Args:
            collection_id: Id of the temporal collection.
            after_block_id: Optional exclusive start block id, for pages in
                block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Entries in ascending block-id order.

        Raises:
            TypeError: If `limit` is not an integer.
            ValueError: If `limit` is negative.
        """

        _validate_limit(limit)
        with self._structure_lock:
            entries = sorted(
                (entry for entry in self._temporal_entries.values() if entry.collection_id == collection_id and (after_block_id is None or entry.block_id > after_block_id)),
                key=lambda entry: entry.block_id,
            )
            return entries[:limit]

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal membership entry, in one atomic step.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        with self._structure_lock:
            slot = (collection_id, block_id)
            if slot not in self._temporal_entries:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection " f"{collection_id!r}.")
            self._temporal_entries.pop(slot)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove the collections that a deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        with self._structure_lock:
            for slot, entry in list(self._temporal_entries.items()):
                if entry.collection_id == block_id or entry.block_id == block_id:
                    self._temporal_entries.pop(slot, None)

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
        """Return one page of the `TemporalIndex` contract that matches the temporal filters.

        Args:
            collection_id: Id of the temporal collection.
            occurred_only: If `True`, only entries with an event time stay.
            occurred_start: Optional inclusive lower bound on event time.
            occurred_stop: Optional exclusive upper bound on event time.
            valid_at: Optional instant that the validity period of an entry
                contains.
            overlaps: Optional validity period that the period of an entry
                overlaps.
            after_block_id: Optional exclusive start block id, for pages in
                block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in ascending block-id order.

        Raises:
            TypeError: If `occurred_only` is not a boolean, a time bound is
                not a datetime, `overlaps` is not a `ValidityInterval`, or
                `limit` is not an integer.
            ValueError: If a time bound is naive, `occurred_stop` is earlier
                than `occurred_start`, or `limit` is negative.
        """

        from genie.structures.records import ValidityInterval

        if not isinstance(occurred_only, bool):
            raise TypeError("occurred_only must be a boolean.")
        occurred_start = _normalize_optional_time(
            occurred_start,
            name="occurred_start",
        )
        occurred_stop = _normalize_optional_time(
            occurred_stop,
            name="occurred_stop",
        )
        valid_at = _normalize_optional_time(valid_at, name="valid_at")
        if (occurred_start is not None and occurred_stop is not None) and occurred_stop < occurred_start:
            raise ValueError("occurred_stop must be greater than or equal to occurred_start.")
        if overlaps is not None and not isinstance(overlaps, ValidityInterval):
            raise TypeError("overlaps must be a ValidityInterval.")
        _validate_limit(limit)
        with self._structure_lock:
            entries = sorted(
                (
                    entry
                    for entry in self._temporal_entries.values()
                    if entry.collection_id == collection_id
                    and (after_block_id is None or entry.block_id > after_block_id)
                    and _temporal_matches(
                        entry,
                        occurred_only=occurred_only,
                        occurred_start=occurred_start,
                        occurred_stop=occurred_stop,
                        valid_at=valid_at,
                        overlaps=overlaps,
                    )
                ),
                key=lambda entry: entry.block_id,
            )
            return entries[:limit]

    def claim_route(self, entry: RouteInfo) -> RouteInfo:
        """Store a route catalog entry only when its block id has no entry.

        Args:
            entry: Block id and owning route to record.

        Returns:
            Stored entry.

        Raises:
            MemoryConflict: If the block id already has an entry.
        """

        with self._structure_lock:
            if entry.block_id in self._route_entries:
                raise MemoryConflict(f"Block id {entry.block_id!r} already has a route catalog entry.")
            self._route_entries[entry.block_id] = entry
            return entry

    def get_route(self, block_id: str) -> RouteInfo | None:
        """Return the route catalog entry of a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored entry, or `None` when the block id has no entry.
        """

        with self._structure_lock:
            return self._route_entries.get(block_id)

    def release_routes(self, block_ids: Sequence[str]) -> None:
        """Remove the route catalog entries of block ids.

        Args:
            block_ids: Block ids whose entries to remove. An id with no entry
                is ignored.
        """

        with self._structure_lock:
            for block_id in block_ids:
                self._route_entries.pop(block_id, None)

    def _require_structure_blocks(
        self,
        *,
        container_id: str,
        block_id: str,
        container_name: str,
    ) -> None:
        """Raise `MemoryConflict` if a structure container or the referenced block does not exist."""

        if container_id not in self._blocks:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if block_id not in self._blocks:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a typed link between existing blocks.

        Args:
            link: Link to store.

        Returns:
            Defensive copy of the stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, or an earlier `add_acyclic_link` protects the relation.
        """

        with self._structure_lock:
            if link.relation in self._acyclic_relations:
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic " "graph; use atomic acyclic insertion.")
            self._validate_new_link(link)
            return self._store_new_link(link)

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Add a link in one atomic step, if its relation stays acyclic.

        - **Adoption:** on the first call, the backend adopts the current links
          with the same relation only when they already form a DAG.
        - **Protection:** after that, ordinary link inserts cannot use the
          relation. The protection stays for the life of the backend.

        Args:
            link: Link to store. Its relation identifies the protected graph of
                the cycle check.

        Returns:
            Defensive copy of the stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, the relation already contains a cycle, or the new link
                makes one.
        """

        with self._structure_lock:
            self._validate_new_link(link)
            if link.relation not in self._acyclic_relations and self._relation_has_cycle(link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or self._link_path_exists(
                link.target_id,
                link.source_id,
                relation=link.relation,
            ):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create " f"a cycle in relation {link.relation!r}.")
            self._acyclic_relations.add(link.relation)
            return self._store_new_link(link)

    def _validate_new_link(self, link: MemoryLink) -> None:
        """Check the link id and the endpoints before the backend stores a link."""

        if link.id in self._links:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if link.source_id not in self._blocks:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if link.target_id not in self._blocks:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _store_new_link(self, link: MemoryLink) -> MemoryLink:
        """Store a checked link while this method holds the structure lock."""

        stored = _copy_link(link)
        self._links[stored.id] = stored
        return _copy_link(stored)

    def _link_path_exists(
        self,
        start_id: str,
        target_id: str,
        *,
        relation: str,
    ) -> bool:
        """Return whether one relation has a directed path between two nodes."""

        pending = deque([start_id])
        visited: set[str] = set()
        while pending:
            current_id = pending.popleft()
            if current_id == target_id:
                return True
            if current_id in visited:
                continue
            visited.add(current_id)
            pending.extend(link.target_id for link in self._links.values() if link.relation == relation and link.source_id == current_id)
        return False

    def _relation_has_cycle(self, relation: str) -> bool:
        """Return whether existing links of one relation contain a cycle."""

        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = defaultdict(int)
        nodes: set[str] = set()
        for link in self._links.values():
            if link.relation != relation:
                continue
            adjacency[link.source_id].append(link.target_id)
            indegree[link.target_id] += 1
            nodes.add(link.source_id)
            nodes.add(link.target_id)
        pending = deque(node_id for node_id in nodes if indegree[node_id] == 0)
        visited = 0
        while pending:
            node_id = pending.popleft()
            visited += 1
            for target_id in adjacency[node_id]:
                indegree[target_id] -= 1
                if indegree[target_id] == 0:
                    pending.append(target_id)
        return visited != len(nodes)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of an existing typed link.

        Args:
            link: Link with the replacement metadata.

        Returns:
            Defensive copy of the stored replacement link.

        Raises:
            MemoryConflict: If the link does not exist, or a field other than
                metadata changed.
        """

        with self._structure_lock:
            previous = self._links.get(link.id)
            if previous is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            link = _copy_link(link)
            self._links[link.id] = link
            return _copy_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block.

        Args:
            block_id: Block id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Defensive copies of matching links.

        Raises:
            ValueError: If `direction` is not valid.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        with self._structure_lock:
            selected: list[MemoryLink] = []
            for link in self._links.values():
                if relation is not None and link.relation != relation:
                    continue
                if (
                    (direction == "out" and link.source_id == block_id)
                    or (direction == "in" and link.target_id == block_id)
                    or (direction == "both" and block_id in {link.source_id, link.target_id})
                ):
                    selected.append(_copy_link(link))
            return selected

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for a block.

        Args:
            block_id: Block id whose connected links to remove.
        """

        with self._structure_lock:
            for link_id, link in list(self._links.items()):
                if link.source_id == block_id or link.target_id == block_id:
                    self._links.pop(link_id, None)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks with portable text and metadata matching.

        Args:
            query: Normalized memory query.

        Returns:
            Search results sorted by descending score.
        """

        with self._structure_lock:
            candidates = self._scope_candidates(query)
            results: list[SearchResult] = []
            text = query.text.casefold() if query.text else None
            for block in candidates:
                if query.types and block.type not in query.types:
                    continue
                if query.filters and not matches_filters(block, query.filters):
                    continue
                score = 1.0
                reason = "matched filters"
                if text:
                    haystack = f"{block.type} {block.value!r} {block.metadata!r}".casefold()
                    if text not in haystack:
                        continue
                    score = float(haystack.count(text) or 1)
                    reason = "matched text"
                results.append(SearchResult(block=_copy_block(block), score=score, reason=reason))
            results.sort(key=lambda result: result.score, reverse=True)
            return results[: query.limit]

    def _scope_candidates(self, query: MemoryQuery) -> list[MemoryBlock]:
        """Return the candidate blocks before the text, type, and filter matching."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return list(self._blocks.values())
        if query.root_id not in self._blocks:
            return []
        if query.scope == Scope.SELF:
            return [self._blocks[query.root_id]]
        if query.scope == Scope.SUBTREE:
            return self.descendants(query.root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            return [self._blocks[query.root_id], *self.ancestors(query.root_id)]
        if query.scope == Scope.LINKS:
            block_ids = [query.root_id]
            seen = {query.root_id}
            for link in self.list_links(query.root_id, direction="both"):
                for block_id in (link.source_id, link.target_id):
                    if block_id not in seen:
                        seen.add(block_id)
                        block_ids.append(block_id)
            return [self._blocks[block_id] for block_id in block_ids if block_id in self._blocks]
        return list(self._blocks.values())

    def _move_parent(self, previous: MemoryBlock, updated: MemoryBlock) -> None:
        """Update the parent indexes, and reject a parent change that is not valid."""

        if updated.parent_id is not None and updated.parent_id not in self._blocks:
            raise MemoryConflict(f"Parent memory block {updated.parent_id!r} does not exist.")
        if updated.parent_id == updated.id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if updated.parent_id is not None and self.contains(updated.id, updated.parent_id):
            raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
        if previous.parent_id is not None and previous.id in self._children.get(previous.parent_id, []):
            self._children[previous.parent_id].remove(previous.id)
        if updated.parent_id is not None:
            self._children[updated.parent_id].append(updated.id)


def _copy_block(block: MemoryBlock) -> MemoryBlock:
    """Return a block copy whose mutable payloads are separate copies."""

    return block.with_updates(
        value=deepcopy(block.value),
        metadata=deepcopy(block.metadata),
    )


def _copy_link(link: MemoryLink) -> MemoryLink:
    """Return a link copy whose mutable metadata is a separate copy."""

    return MemoryLink(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        relation=link.relation,
        metadata=deepcopy(link.metadata),
        created_at=link.created_at,
    )


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(
    previous: MemoryBlock,
    replacement: MemoryBlock,
    expected_version: int | None,
) -> None:
    """Check one optional compare-and-swap block replacement."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version " f"{expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _key_slot(
    collection_id: str,
    address: MemoryAddress,
) -> tuple[str, tuple[str, ...], str]:
    """Return the unique in-memory slot for a key binding."""

    return collection_id, address.namespace, address.key


def _namespace_matches(
    candidate: tuple[str, ...],
    selected: tuple[str, ...] | None,
    *,
    recursive: bool,
) -> bool:
    """Return whether a namespace matches an exact selection or a prefix selection."""

    if selected is None:
        return True
    if recursive:
        return candidate[: len(selected)] == selected
    return candidate == selected


def _validate_sequence(value: object, *, name: str) -> None:
    """Raise an error if a sequence value is not a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_limit(value: object) -> None:
    """Raise an error if a page limit is not a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer.")
    if value < 0:
        raise ValueError("limit must be greater than or equal to zero.")


def _temporal_matches(
    entry: TemporalEntry,
    *,
    occurred_only: bool,
    occurred_start: datetime | None,
    occurred_stop: datetime | None,
    valid_at: datetime | None,
    overlaps: ValidityInterval | None,
) -> bool:
    """Return whether an entry matches all temporal index filters."""

    occurred_at = entry.occurred_at
    if occurred_only and occurred_at is None:
        return False
    if occurred_start is not None and (occurred_at is None or occurred_at < occurred_start):
        return False
    if occurred_stop is not None and (occurred_at is None or occurred_at >= occurred_stop):
        return False
    validity = entry.validity
    if valid_at is not None:
        if validity is None or validity.valid_at > valid_at:
            return False
        if validity.invalid_at is not None and valid_at >= validity.invalid_at:
            return False
    if overlaps is not None:
        if validity is None:
            return False
        if validity.invalid_at is not None and validity.invalid_at <= overlaps.valid_at:
            return False
        if overlaps.invalid_at is not None and overlaps.invalid_at <= validity.valid_at:
            return False
    return True


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Check an optional timezone-aware datetime, and return it in UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)
