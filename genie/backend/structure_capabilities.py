"""Optional backend protocols for DAG, keyed, ordered, and temporal structures."""

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
    for that relation. After a relation is protected, ordinary ``add_link``
    calls must reject the relation or apply the same cycle check.
    """

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Atomically add a link unless it would close a directed cycle.

        Existing links with the same relation are part of the graph. The first
        protected insertion may adopt them only when they are already acyclic.

        Raises:
            MemoryConflict: If the relation already contains a cycle or the
                new link would create one.
        """

        ...


@runtime_checkable
class KeyedStore(Protocol):
    """Atomic persistence interface for namespaced key bindings.

    Implementations must make bind, compare-and-swap rebind, and conditional
    unbind operations atomic for one ``(collection_id, address)`` pair.
    """

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Create a binding only when its address is unused.

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

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        ...

    def get_key(
        self,
        collection_id: str,
        address: MemoryAddress,
    ) -> KeyBinding | None:
        """Return an exact binding, or ``None`` when its address is unused."""

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
        """List one bounded page of bindings in stable address order."""

        ...

    def unbind_key(
        self,
        collection_id: str,
        address: MemoryAddress,
        *,
        expected_block_id: str,
    ) -> None:
        """Remove a binding only when its current block id matches.

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        ...

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to a deleted block."""

        ...


@runtime_checkable
class SequenceStore(Protocol):
    """Atomic persistence interface for ordered append-only logs.

    Implementations must allocate one unique, monotonically increasing
    sequence number for every append to a given log.
    """

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Atomically allocate a sequence and append a block reference."""

        ...

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry, or ``None`` when it is absent."""

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
        """List one bounded page in an optional half-open sequence range."""

        ...

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove logs owned by and entries pointing to a deleted block."""

        ...


@runtime_checkable
class TemporalStore(Protocol):
    """Persistence interface for temporal collection membership.

    Entries are unique by ``(collection_id, block_id)``. Implementations must
    return list results in ascending block-id order so callers can scan in
    bounded pages without relying on a database-specific cursor.
    """

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Add a temporal entry only when the block is not already present.

        Raises:
            MemoryConflict: If the collection already contains the block.
        """

        ...

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Replace an existing temporal entry for the same collection and block.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        ...

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Return an exact temporal entry, or ``None`` when it is absent."""

        ...

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """List one bounded page in stable ascending block-id order."""

        ...

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        ...

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove collections owned by and entries pointing to a deleted block."""

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
        """Return one bounded page matching the supplied temporal filters."""

        ...
