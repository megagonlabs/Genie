"""Optional backend protocols for DAG, keyed, ordered, and temporal structures.

- **Optional extensions:** the protocols here are optional extensions to
  `MemoryBackend`.
- **Early failure:** structure facades depend on them, not on concrete
  backend classes. A facade that takes a store checks for the capability when
  it is built. So a backend without the capability fails early with
  `UnsupportedOperation`. `LineageDAG` takes no store, so its first `add`
  raises `UnsupportedOperation` instead.
- **Atomic operations:** each protocol covers an operation that is not safe
  as a portable read followed by a write. The contract requires that
  implementations serialize the read and the write, so concurrent callers
  cannot interleave them.
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
    """Atomic insert interface for directed acyclic graphs of one relation.

    - **One graph for each relation:** the exact link relation identifies one
      protected graph.
    - **Explicit protection:** only `protect_relation` starts the protection
      of a relation. `add_acyclic_link` never starts it.
    - **Serialized check:** the contract requires that implementations
      serialize the reachability check and the insert with each link write for
      that relation.
    - **Protected relation:** after a relation is protected, an ordinary
      `add_link` call rejects the relation or applies the same cycle check.
    """

    def protect_relation(self, relation: str) -> None:
        """Protect a relation as an acyclic graph, in one atomic step.

        - **Current links:** the protection adopts the stored links with the
          same relation only when they already form a DAG.
        - **Repeated call:** a call for a protected relation changes nothing.
        - **Permanent:** the protection stays for the life of the stored data.

        Args:
            relation: Relation that identifies the protected graph.

        Raises:
            MemoryConflict: If the stored links of the relation contain a
                cycle. The relation then stays without protection.
        """

        ...

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Add a link to a protected relation in one atomic step, unless it closes a directed cycle.

        Args:
            link: Link to store. Its relation identifies the protected graph of
                the cycle check.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the relation is not protected, or the new link
                would create a cycle.
        """

        ...


@runtime_checkable
class KeyedStore(Protocol):
    """Atomic persistence interface for namespaced key bindings.

    The contract requires that implementations make the bind, the
    compare-and-swap rebind, and the conditional unbind atomic for one
    `(collection_id, address)` pair.
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
            expected_block_id: Block id that the binding points to now. The
                replacement applies only if this id matches.

        Returns:
            Stored replacement binding.

        Raises:
            MemoryConflict: If the address is not bound or points to a
                different block.
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
            namespace: Optional exact namespace filter. With `recursive=True`,
                it is a namespace prefix.
            recursive: If `True`, the result also includes the nested
                namespaces below `namespace`.
            after: Optional exclusive start address, for pages in address
                order.
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
            expected_block_id: Block id that the binding points to now. The
                removal applies only if this id matches.

        Raises:
            MemoryConflict: If the address is not bound or points to a
                different block.
        """

        ...

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove the bindings that a deleted block owns, or that point to it.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        ...


@runtime_checkable
class SequenceStore(Protocol):
    """Atomic storage interface for ordered append-only logs.

    - **Sequence numbers:** the contract requires one unique, monotonically
      increasing sequence number for each append to a given log.
    - **No uniqueness key:** an append has no uniqueness key. So an adapter
      that retries a commit whose reply was lost can append the same block
      twice, at 2 sequences.
    - **Exactly-once appends:** an application that needs them puts its own
      token in the appended block, and skips a duplicate on read.
    - **Adapter guides:** each adapter README tells what its retry repeats.
    """

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Allocate a sequence and append a block reference, in one atomic step.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry with the allocated sequence number.
        """

        ...

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry, or `None` when it does not exist.

        Args:
            log_id: Id of the ordered log.
            sequence: Exact position in the log.

        Returns:
            Stored entry, or `None` when the position has no entry.
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
            reverse: If `True`, the entries come in descending sequence order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in sequence order, descending when `reverse` is
            `True`.
        """

        ...

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove the logs that a deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. Entries whose log id or target
                block id match are removed.
        """

        ...


@runtime_checkable
class TemporalStore(Protocol):
    """Storage interface for temporal collection membership.

    - **Unique entries:** entries are unique by `(collection_id, block_id)`.
    - **Order:** the contract requires list results in ascending block-id
      order. So callers can scan in bounded pages with no database-specific
      cursor.
    """

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Add a temporal entry only when the block is not already in the collection.

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
        """Return an exact temporal entry, or `None` when it does not exist.

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
            after_block_id: Optional exclusive start block id, for pages in
                block-id order.
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
        """Remove the collections that a deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        ...


@runtime_checkable
class TemporalIndex(Protocol):
    """Optional index that filters temporal collection entries first.

    - **Filters and order:** the contract requires that implementations apply
      all given filters, and return entries in ascending block-id order.
    - **Half-open:** bounds and validity intervals are half-open.
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
        """Return one bounded page that matches the given temporal filters.

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
        """

        ...
