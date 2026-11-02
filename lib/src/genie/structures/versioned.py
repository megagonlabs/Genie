"""Versioned records made from keyed identities and ordered logs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genie.backend.base import BlockRouter
from genie.backend.structure_capabilities import KeyedStore, SequenceStore
from genie.hooks import HookRegistry
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryNotFound, UnsupportedOperation
from genie.structures._hooks import resolve_hooks
from genie.structures._routing import block_id as _block_id
from genie.structures._routing import resolve_store
from genie.structures.access import AccessChecker, BlockReader, BlockRef
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
class VersionedAccess(BlockReader, AccessChecker, Protocol):
    """Small access surface that `VersionedRecords` requires."""


@runtime_checkable
class VersionedStore(KeyedStore, SequenceStore, Protocol):
    """Combined existing capabilities that `VersionedRecords` requires."""


class VersionedRecords:
    """Stable keyed identities with ordered immutable revision references.

    - **Identity:** a namespaced address maps to one existing identity block.
    - **Revision log:** the identity block is also the anchor of an event log of
      existing revision blocks. Log sequence zero maps to logical revision one.
    - **Gaps:** a deleted revision block removes its log entry, and the
      facade does not use that revision number again.
    - **No payload changes:** the facade never rebinds identities. It does not
      create, update, or delete payload blocks.
    - **Writes:** `register` needs `write` on the collection block. `append`
      needs `write` on the identity block, because the identity is the anchor
      of the revision log.
    - **Reads:** a read needs only `read` on those blocks and on each revision.
      If the principal cannot read the collection or the identity, a read
      raises `AccessDenied`. A listing and `current` skip revisions that the
      principal cannot read. They get each revision with `get`, so the access
      object reports each unreadable revision to `on_denied`.
    """

    __slots__ = ("_access", "_hooks", "_identities", "_store", "collection_id")

    def __init__(
        self,
        access: VersionedAccess,
        store: VersionedStore | BlockRouter,
        *,
        collection: BlockRef,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Create a versioned-record collection.

        Args:
            access: Full or scoped memory access that resolves all blocks.
            store: Backend that implements the keyed and sequence capabilities,
                or a routing backend that resolves to the route that holds the
                collection.
            collection: Block or id that is the anchor of the record address
                space.
            hooks: Optional registry for the reused keyed collection and event
                logs. Defaults to the registry of the access object.

        Raises:
            AccessDenied: If the principal cannot read the collection block.
            MemoryNotFound: If the collection block does not exist.
            UnsupportedOperation: If the store does not have both required
                capabilities.
        """

        missing_message = f"Versioned collection block {_block_id(collection)!r} does not exist."
        stored_collection = access.get(collection)
        if stored_collection is None:
            raise MemoryNotFound(missing_message)
        store = resolve_store(store, stored_collection, missing_message=missing_message)
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
            collection=stored_collection,
            hooks=self._hooks,
        )
        self.collection_id = self._identities.collection_id

    @property
    def collection(self) -> MemoryBlock:
        """Return the current visible collection block."""

        return self._identities.collection

    def register(self, address: MemoryAddress, identity: BlockRef) -> KeyedItem:
        """Bind an unused address to a stable identity block, in one atomic step.

        - **Anchor:** the identity is also the anchor of the revision log of this
          record.
        - **Separate first append:** the first revision is a separate `append`
          call. So the registration never claims a transaction across 2
          structures.

        Args:
            address: Stable namespace and key for the logical record.
            identity: Existing visible block that identifies the record.

        Returns:
            Created key binding and resolved identity block.

        Raises:
            AccessDenied: If the collection denies `write` or the identity
                denies `read`.
            MemoryConflict: If the address is already registered.
            MemoryNotFound: If the collection or the identity block does not
                exist.
        """

        return self._identities.bind(address, identity)

    def identity(self, address: MemoryAddress) -> MemoryBlock | None:
        """Return the visible stable identity at an exact address.

        Args:
            address: Address of a registered stable identity.

        Returns:
            Visible identity block, or `None` when the address is not
            registered.

        Raises:
            AccessDenied: If the principal cannot read the collection or the
                identity block.
            MemoryNotFound: If the address is bound to a block that no longer
                exists.
        """

        return self._identities.get(address)

    def identities(
        self,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        limit: int = 100,
    ) -> list[KeyedItem]:
        """List visible registered identities in stable address order.

        Args:
            namespace: Optional exact namespace. With `recursive=True`, it is
                a namespace prefix.
            recursive: If `True`, the list also includes the nested namespaces
                below `namespace`.
            limit: Maximum visible identities to return.

        Returns:
            Visible identities in ascending address order. The list leaves out
            identities that do not exist or that the principal cannot read.

        Raises:
            AccessDenied: If the principal cannot read the collection.
            TypeError: If `recursive` is not a boolean or `limit` is not an
                integer.
            ValueError: If `limit` is negative.
        """

        return self._identities.items(
            namespace=namespace,
            recursive=recursive,
            limit=limit,
        )

    def append(self, address: MemoryAddress, block: BlockRef) -> RevisionItem:
        """Append an existing block as the next logical revision, in one atomic step.

        Args:
            address: Address of a registered stable identity.
            block: Existing visible block that holds the new revision value.

        Returns:
            Allocated positive revision number and resolved revision block.

        Raises:
            AccessDenied: If the principal cannot read the collection or the
                identity, the identity denies `write`, or the block denies
                `read`.
            MemoryNotFound: If no identity is registered at `address`, or the
                block does not exist.
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
        """Return the newest visible revision at an address, if one exists.

        Args:
            address: Address of a registered stable identity.

        Returns:
            Newest visible revision, or `None` when the address is not
            registered or has no visible revision.

        Raises:
            AccessDenied: If the principal cannot read the collection or the
                identity block.
        """

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
        """Return one exact visible logical revision, if it exists.

        Args:
            address: Address of a registered stable identity.
            revision: Positive logical revision number.

        Returns:
            Matching revision, or `None` when the address is not registered or
            the revision does not exist.

        Raises:
            AccessDenied: If the principal cannot read the collection, the
                identity block, or the revision block.
            TypeError: If `revision` is not an integer.
            ValueError: If `revision` is less than one.
        """

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
            reverse: If `True`, the newest revisions come first.
            limit: Maximum visible revisions to return.

        Returns:
            Visible revisions in ascending or descending revision order. The
            list leaves out targets that do not exist or that the principal
            cannot read.

        Raises:
            AccessDenied: If the principal cannot read the collection or the
                identity block.
            TypeError: If `reverse` is not a boolean, or `start`, `stop`, or
                `limit` is not an integer.
            ValueError: If `start` or `stop` is less than one, `limit` is
                negative, or `stop` is less than `start`.
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
        """Return the visible revision log of a registered identity, or `None`."""

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
    """Change a zero-based log item into a positive logical revision."""

    return RevisionItem(
        entry=RevisionEntry(
            record_id=item.entry.log_id,
            revision=item.entry.sequence + 1,
            block_id=item.entry.block_id,
        ),
        block=item.block,
    )


def _validate_revision(value: object, *, name: str) -> None:
    """Raise an error if a logical revision number is not a positive integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 1:
        raise ValueError(f"{name} must be greater than or equal to one.")


def _validate_limit(limit: object) -> None:
    """Raise an error if a result limit is not a non-negative integer."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
