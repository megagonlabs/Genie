"""Versioned records composed from keyed identities and ordered logs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genie.backend.structure_capabilities import KeyedStore, SequenceStore
from genie.hooks import HookRegistry
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryNotFound, UnsupportedOperation
from genie.structures._hooks import resolve_hooks
from genie.structures.access import BlockReader, BlockRef
from genie.structures.keyed import KeyedCollection
from genie.structures.log import EventLog
from genie.structures.records import (
    KeyedItem,
    LogItem,
    MemoryAddress,
    RevisionEntry,
    RevisionItem,
)


@runtime_checkable
class VersionedAccess(BlockReader, Protocol):
    """Small access surface required by ``VersionedRecords``."""


@runtime_checkable
class VersionedStore(KeyedStore, SequenceStore, Protocol):
    """Combined existing capabilities required by ``VersionedRecords``."""


class VersionedRecords:
    """Stable keyed identities with ordered immutable revision references.

    A namespaced address maps to one existing identity block. That identity
    block also anchors an event log of existing revision blocks. Log sequence
    zero maps to logical revision one. The facade never rebinds identities and
    does not create, update, or delete payload blocks.
    """

    __slots__ = ("_access", "_hooks", "_identities", "_store", "collection_id")

    def __init__(
        self,
        access: VersionedAccess,
        store: VersionedStore,
        *,
        collection: BlockRef,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Create a versioned-record collection.

        Args:
            access: Full or scoped memory access used to resolve every block.
            store: Backend implementing keyed and sequence capabilities.
            collection: Block or id that owns the record address space.
            hooks: Optional registry forwarded to the reused keyed collection
                and event logs. Defaults to the access object's registry.

        Raises:
            MemoryNotFound: If the collection block is missing.
            UnsupportedOperation: If the store lacks either required capability.
        """

        if not isinstance(store, VersionedStore):
            raise UnsupportedOperation(
                "The configured store does not support keyed versioned records."
            )
        self._access = access
        self._store = store
        self._hooks = resolve_hooks(access, hooks)
        self._identities = KeyedCollection(
            access,
            store,
            collection=collection,
            hooks=self._hooks,
        )
        self.collection_id = self._identities.collection_id

    @property
    def collection(self) -> MemoryBlock:
        """Return the current visible collection block."""

        return self._identities.collection

    def register(self, address: MemoryAddress, identity: BlockRef) -> KeyedItem:
        """Atomically bind an unused address to a stable identity block.

        The identity is also the anchor for this record's revision log. The
        first revision is appended separately so registration never claims a
        cross-structure transaction.

        Args:
            address: Stable namespace and key for the logical record.
            identity: Existing visible block that identifies the record.

        Returns:
            Created key binding and resolved identity block.

        Raises:
            MemoryConflict: If the address is already registered.
        """

        return self._identities.bind(address, identity)

    def identity(self, address: MemoryAddress) -> MemoryBlock | None:
        """Return the visible stable identity at an exact address."""

        return self._identities.get(address)

    def identities(
        self,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        limit: int = 100,
    ) -> list[KeyedItem]:
        """List visible registered identities in stable address order."""

        return self._identities.items(
            namespace=namespace,
            recursive=recursive,
            limit=limit,
        )

    def append(self, address: MemoryAddress, block: BlockRef) -> RevisionItem:
        """Atomically append an existing block as the next logical revision.

        Args:
            address: Address of a registered stable identity.
            block: Existing visible block holding the new revision value.

        Returns:
            Allocated positive revision number and resolved revision block.

        Raises:
            MemoryNotFound: If no identity is registered at ``address``.
        """

        identity = self.identity(address)
        if identity is None:
            raise MemoryNotFound(
                f"Versioned record address {address!r} is not registered."
            )
        item = EventLog(
            self._access,
            self._store,
            log=identity,
            hooks=self._hooks,
        ).append(block)
        return _revision_item(item)

    def current(self, address: MemoryAddress) -> RevisionItem | None:
        """Return the newest visible revision at an address, if one exists."""

        log = self._log(address)
        if log is None:
            return None
        items = log.read(reverse=True, limit=1)
        return _revision_item(items[0]) if items else None

    def revision(
        self,
        address: MemoryAddress,
        revision: int,
    ) -> RevisionItem | None:
        """Return one exact visible logical revision, if it exists."""

        _validate_revision(revision, name="revision")
        log = self._log(address)
        if log is None:
            return None
        item = log.get(revision - 1)
        return _revision_item(item) if item is not None else None

    def history(
        self,
        address: MemoryAddress,
        *,
        start: int = 1,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[RevisionItem]:
        """Return visible revisions from a half-open logical revision range.

        Args:
            address: Address of a registered stable identity.
            start: Inclusive positive logical revision number.
            stop: Optional exclusive positive logical revision number.
            reverse: Whether to return newest revisions first.
            limit: Maximum visible revisions to return.

        Returns:
            Visible revisions in ascending or descending revision order.
        """

        _validate_revision(start, name="start")
        if stop is not None:
            _validate_revision(stop, name="stop")
            if stop < start:
                raise ValueError("stop must be greater than or equal to start.")
        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean.")
        _validate_limit(limit)
        log = self._log(address)
        if log is None:
            return []
        items = log.read(
            start=start - 1,
            stop=stop - 1 if stop is not None else None,
            reverse=reverse,
            limit=limit,
        )
        return [_revision_item(item) for item in items]

    def _log(self, address: MemoryAddress) -> EventLog | None:
        """Return the visible revision log for a registered identity."""

        identity = self.identity(address)
        if identity is None:
            return None
        return EventLog(
            self._access,
            self._store,
            log=identity,
            hooks=self._hooks,
        )


def _revision_item(item: LogItem) -> RevisionItem:
    """Convert a zero-based log item to a positive logical revision."""

    return RevisionItem(
        entry=RevisionEntry(
            record_id=item.entry.log_id,
            revision=item.entry.sequence + 1,
            block_id=item.entry.block_id,
        ),
        block=item.block,
    )


def _validate_revision(value: object, *, name: str) -> None:
    """Require a positive integer logical revision number."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 1:
        raise ValueError(f"{name} must be greater than or equal to one.")


def _validate_limit(limit: object) -> None:
    """Require a non-negative integer result limit."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
