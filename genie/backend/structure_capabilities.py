"""Optional backend protocols for DAG, keyed, ordered, and temporal structures.

The protocols here are optional extensions to `MemoryBackend`. Structure
facades depend on them instead of on concrete backend classes, and they check
for the capability at construction time so a backend that does not implement
one fails early with `UnsupportedOperation`.

Each protocol covers an operation that cannot be implemented safely as a
portable read followed by a write. Implementations must serialize the read and
the write so concurrent callers cannot interleave them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from genie.memory.links import MemoryLink
    from genie.structures.records import (
        KeyBinding,
        LogEntry,
        MemoryAddress,
        TemporalEntry,
        ValidityInterval,
    )


@runtime_checkable
class AcyclicLinkStore(Protocol):
    """Atomic insertion interface for relation-scoped directed acyclic graphs.

    The exact link relation identifies one protected graph. Implementations
    must serialize the reachability check and insertion with every link write
    for that relation. After a relation is protected, ordinary `add_link`
    calls must reject the relation or apply the same cycle check.
    """

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Atomically add a link unless it would close a directed cycle.

        Existing links with the same relation are part of the graph. The first
        protected insertion may adopt them only when they are already acyclic.

        Args:
            link: Link to store. Its relation identifies the protected graph
                the cycle check applies to.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the relation already contains a cycle or the
                new link would create one.
        """

        ...


@runtime_checkable
class KeyedStore(Protocol):
    """Atomic persistence interface for namespaced key bindings.

    Implementations must make bind, compare-and-swap rebind, and conditional
    unbind operations atomic for one `(collection_id, address)` pair.
    """

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Create a binding only when its address is unused.

        Args:
            binding: Collection id, address, and target block id to store.

        Returns:
            Stored binding.

        Raises:
            MemoryConflict: If the address is already bound.
        """

        ...

    def rebind_key(
        self,
        binding: KeyBinding,
        *,
        expected_block_id: str,
    ) -> KeyBinding:
        """Replace a binding only when its current block id matches.

        Args:
            binding: Replacement binding. Its collection id and address select
                the existing binding to replace.
            expected_block_id: Block id the binding must currently point to
                for the replacement to apply.

        Returns:
            Stored replacement binding.

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        ...

    def get_key(
        self,
        collection_id: str,
        address: MemoryAddress,
    ) -> KeyBinding | None:
        """Return an exact binding, or `None` when its address is unused.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to look up.

        Returns:
            Stored binding, or `None` when the address is unused.
        """

        ...

    def list_keys(
        self,
        collection_id: str,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        after: MemoryAddress | None = None,
        limit: int = 100,
    ) -> list[KeyBinding]:
        """List one bounded page of bindings in stable address order.

        Args:
            collection_id: Id of the collection to list.
            namespace: Optional exact namespace filter. With `recursive=True`
                it is treated as a namespace prefix.
            recursive: Whether to include nested namespaces below `namespace`.
            after: Optional exclusive start address used to page through
                results in address order.
            limit: Maximum number of bindings to return.

        Returns:
            Matching bindings in stable ascending address order.
        """

        ...

    def unbind_key(
        self,
        collection_id: str,
        address: MemoryAddress,
        *,
        expected_block_id: str,
    ) -> None:
        """Remove a binding only when its current block id matches.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to remove.
            expected_block_id: Block id the binding must currently point to
                for the removal to apply.

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        ...

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to a deleted block.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        ...


@runtime_checkable
class SequenceStore(Protocol):
    """Atomic persistence interface for ordered append-only logs.

    Implementations must allocate one unique, monotonically increasing
    sequence number for every append to a given log.

    An append carries no uniqueness key, so an adapter that retries a commit
    whose reply was lost can append the same block twice, at two sequences.

    - An application that needs exactly-once appends should carry its own token
      in the appended block and skip a duplicate on read.
    - Each adapter README states what its retry repeats.
    """

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Atomically allocate a sequence and append a block reference.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry carrying the allocated sequence number.
        """

        ...

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry, or `None` when it is absent.

        Args:
            log_id: Id of the ordered log.
            sequence: Exact position within the log.

        Returns:
            Stored entry, or `None` when the position holds no entry.
        """

        ...

    def list_log_entries(
        self,
        log_id: str,
        *,
        start: int | None = None,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[LogEntry]:
        """List one bounded page in an optional half-open sequence range.

        Args:
            log_id: Id of the ordered log.
            start: Optional inclusive lower sequence bound.
            stop: Optional exclusive upper sequence bound.
            reverse: Whether to return entries in descending sequence order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in sequence order, descending when `reverse` is
            `True`.
        """

        ...

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove logs owned by and entries pointing to a deleted block.

        Args:
            block_id: Id of the deleted block. Entries whose log id or target
                block id match are removed.
        """

        ...


@runtime_checkable
class TemporalStore(Protocol):
    """Persistence interface for temporal collection membership.

    Entries are unique by `(collection_id, block_id)`. Implementations must
    return list results in ascending block-id order so callers can scan in
    bounded pages without relying on a database-specific cursor.
    """

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Add a temporal entry only when the block is not already present.

        Args:
            entry: Collection id, block id, and event time or validity period
                to store.

        Returns:
            Stored entry.

        Raises:
            MemoryConflict: If the collection already contains the block.
        """

        ...

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Replace an existing temporal entry for the same collection and block.

        Args:
            entry: Replacement entry. Its collection id and block id select the
                existing entry to replace.

        Returns:
            Stored replacement entry.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        ...

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Return an exact temporal entry, or `None` when it is absent.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block.

        Returns:
            Stored entry, or `None` when the collection does not contain the
            block.
        """

        ...

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """List one bounded page in stable ascending block-id order.

        Args:
            collection_id: Id of the temporal collection.
            after_block_id: Optional exclusive start block id used to page
                through results in block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Entries in ascending block-id order.
        """

        ...

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        ...

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove collections owned by and entries pointing to a deleted block.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        ...


@runtime_checkable
class TemporalIndex(Protocol):
    """Optional index for prefiltering temporal collection entries.

    Implementations must apply all supplied filters and return entries in
    ascending block-id order. Bounds and validity intervals use half-open
    semantics.
    """

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
        """Return one bounded page matching the supplied temporal filters.

        Args:
            collection_id: Id of the temporal collection.
            occurred_only: Whether to keep only entries that carry an event
                time.
            occurred_start: Optional inclusive lower bound on event time.
            occurred_stop: Optional exclusive upper bound on event time.
            valid_at: Optional instant that an entry's validity period must
                contain.
            overlaps: Optional validity period an entry's own period must
                overlap.
            after_block_id: Optional exclusive start block id used to page
                through results in block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in ascending block-id order.
        """

        ...
