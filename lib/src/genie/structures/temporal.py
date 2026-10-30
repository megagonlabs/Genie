"""Temporal collection facade with portable and indexed scans."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from genie.backend.base import BlockRouter
from genie.backend.structure_capabilities import TemporalIndex, TemporalStore
from genie.hooks import HookOperation, HookRegistry, StructureAction, StructureChange, StructureKind
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryNotFound, UnsupportedOperation
from genie.policy.engine import AccessDenied, Operation
from genie.structures._hooks import (
    after_change,
    before_change,
    context_attributes,
    hook_operation,
    principal_id,
    resolve_hooks,
)
from genie.structures._routing import resolve_store
from genie.structures.access import AccessChecker, BlockReader, BlockRef
from genie.structures.records import TemporalEntry, TemporalItem, ValidityInterval


@runtime_checkable
class TemporalAccess(BlockReader, AccessChecker, Protocol):
    """Small access surface that `TemporalCollection` requires."""


class TemporalCollection:
    """A time-filtered view over existing memory blocks.

    - **Separate membership:** temporal membership is separate from block
      ownership and values.
    - **One entry for each block:** a collection attaches one event time, one
      validity period, or both to a block.
    - **Scan or index:** queries scan the portable store, unless an index is
      given or the store is also a `TemporalIndex`.
    - **Same checks on both paths:** both paths resolve results through normal
      memory access, so the scope and the block policies stay in effect. The
      facade also checks each entry against the filters again, also for index
      results.
    - **Reads:** a read needs `read` on the collection block and on each member.
    - **Changes:** `add`, `replace`, and `remove` also need `write` on the
      collection block. The access object checks it. So a principal that can
      only read the collection cannot change its membership.
    - **Unreadable blocks:** `get` raises `AccessDenied` for a block that the
      principal cannot read, also when the block is not a member. So `get`
      does not show the membership of a hidden block. The scan methods skip
      unreadable members. They get each member with `get`, so the access
      object reports each unreadable member to `on_denied`.
    """

    __slots__ = (
        "_access",
        "_attributes",
        "_hooks",
        "_index",
        "_principal_id",
        "_store",
        "collection_id",
    )

    def __init__(
        self,
        access: TemporalAccess,
        store: TemporalStore | BlockRouter,
        *,
        collection: BlockRef,
        index: TemporalIndex | BlockRouter | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Create a temporal view with a visible collection block as its anchor.

        Args:
            access: Full or scoped memory access that resolves blocks.
            store: Backend capability that stores temporal membership, or a
                routing backend that resolves to the route that holds the
                collection.
            collection: Block or id that identifies this temporal collection.
            index: Optional efficient temporal query index. Without one, the
                facade uses the store as the index if the store implements
                `TemporalIndex`. If not, queries use a portable paged scan. A
                routing backend resolves to the route that holds the
                collection.
            hooks: Optional registry. Defaults to the registry of a `Memory` or
                `MemoryHandle` access object.

        Raises:
            AccessDenied: If the principal cannot read the collection block.
            MemoryNotFound: If the collection block does not exist.
            UnsupportedOperation: If the store or the index does not support
                the needed capability.
        """

        missing_message = f"Temporal collection block {_block_id(collection)!r} does not exist."
        store = resolve_store(store, collection, missing_message=missing_message)
        if not isinstance(store, TemporalStore):
            raise UnsupportedOperation("The configured store does not support temporal collections.")
        if index is not None:
            index = resolve_store(index, collection, missing_message=missing_message)
            if not isinstance(index, TemporalIndex):
                raise UnsupportedOperation("The configured index does not support temporal queries.")
        stored_collection = access.get(collection)
        if stored_collection is None:
            raise MemoryNotFound(f"Temporal collection block {_block_id(collection)!r} does not exist.")
        self._access = access
        self._store = store
        self._hooks = resolve_hooks(access, hooks)
        self._principal_id = principal_id(access)
        self._attributes = context_attributes(access)
        self._index: TemporalIndex | None
        if index is not None:
            self._index = index
        elif isinstance(store, TemporalIndex):
            self._index = store
        else:
            self._index = None
        self.collection_id = stored_collection.id

    @property
    def collection(self) -> MemoryBlock:
        """Return the current visible collection block."""

        return self._require_collection()

    def add(
        self,
        block: BlockRef,
        *,
        occurred_at: datetime | None = None,
        validity: ValidityInterval | None = None,
    ) -> TemporalItem:
        """Attach time information to an existing visible block.

        Args:
            block: Existing memory block or id.
            occurred_at: Optional time when an event happened.
            validity: Optional half-open period in which the information is
                valid.

        Returns:
            Created temporal entry and resolved block.

        Raises:
            AccessDenied: If the collection denies `write` or the block denies
                `read`.
            MemoryConflict: If the block is already in this collection.
            MemoryNotFound: If the collection or the block does not exist.
            TypeError: If `occurred_at` is not a datetime or `validity` is not
                a `ValidityInterval`.
            ValueError: If `occurred_at` and `validity` are both `None`, or
                `occurred_at` is naive.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_ADD,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_writable_collection()
            stored_block = self._require_block(block)
            proposed = TemporalEntry(
                collection_id=self.collection_id,
                block_id=stored_block.id,
                occurred_at=occurred_at,
                validity=validity,
            )
            change = StructureChange(
                kind=StructureKind.TEMPORAL_COLLECTION,
                action=StructureAction.ADD,
                container_id=self.collection_id,
                block_id=stored_block.id,
                after=proposed,
            )
            before_change(self._hooks, context, change)
            entry = self._store.add_temporal_entry(proposed)
            after_change(self._hooks, context, replace(change, after=entry))
            return TemporalItem(entry=entry, block=stored_block)

    def replace(
        self,
        block: BlockRef,
        *,
        occurred_at: datetime | None = None,
        validity: ValidityInterval | None = None,
    ) -> TemporalItem:
        """Replace all time information for an existing temporal member.

        Args:
            block: Existing visible temporal member.
            occurred_at: Replacement event time, or `None` for no event time.
            validity: Replacement validity period, or `None` for no validity
                period.

        Returns:
            Replacement temporal entry and resolved block.

        Raises:
            AccessDenied: If the collection denies `write` or the block denies
                `read`.
            MemoryConflict: If the block is not in this collection.
            MemoryNotFound: If the collection or the block does not exist.
            TypeError: If `occurred_at` is not a datetime or `validity` is not
                a `ValidityInterval`.
            ValueError: If `occurred_at` and `validity` are both `None`, or
                `occurred_at` is naive.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_REPLACE,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_writable_collection()
            stored_block = self._require_block(block)
            current = self._store.get_temporal_entry(
                self.collection_id,
                stored_block.id,
            )
            proposed = TemporalEntry(
                collection_id=self.collection_id,
                block_id=stored_block.id,
                occurred_at=occurred_at,
                validity=validity,
            )
            change = StructureChange(
                kind=StructureKind.TEMPORAL_COLLECTION,
                action=StructureAction.REPLACE,
                container_id=self.collection_id,
                block_id=stored_block.id,
                before=current,
                after=proposed,
            )
            before_change(self._hooks, context, change)
            entry = self._store.replace_temporal_entry(proposed)
            after_change(self._hooks, context, replace(change, after=entry))
            return TemporalItem(entry=entry, block=stored_block)

    def get(self, block: BlockRef) -> TemporalItem | None:
        """Return one exact visible temporal member, if present.

        Args:
            block: Memory block or id to look up.

        Returns:
            Temporal item with the entry and its block, or `None` when the
            collection does not contain the block.

        Raises:
            AccessDenied: If the principal cannot read the collection or the
                block. This check comes before the membership lookup. So the
                call does not show if a hidden block is a member. A rooted
                handle treats an unknown block id as outside the handle.
            MemoryNotFound: If the entry refers to a block that no longer
                exists.
        """

        self._require_collection()
        stored_block = self._access.get(block)
        entry = self._store.get_temporal_entry(self.collection_id, _block_id(block))
        if entry is None:
            return None
        if stored_block is None:
            raise MemoryNotFound(f"Temporal entry in {entry.collection_id!r} references missing " f"memory block {entry.block_id!r}.")
        return TemporalItem(entry=entry, block=stored_block)

    def remove(self, block: BlockRef) -> None:
        """Remove temporal membership. The memory block stays.

        Args:
            block: Existing visible temporal member.

        Raises:
            AccessDenied: If the collection denies `write` or the block denies
                `read`.
            MemoryConflict: If the block is not in this collection.
            MemoryNotFound: If the collection or the block does not exist.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_REMOVE,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_writable_collection()
            stored_block = self._require_block(block)
            current = self._store.get_temporal_entry(
                self.collection_id,
                stored_block.id,
            )
            change = StructureChange(
                kind=StructureKind.TEMPORAL_COLLECTION,
                action=StructureAction.REMOVE,
                container_id=self.collection_id,
                block_id=stored_block.id,
                before=current,
            )
            before_change(self._hooks, context, change)
            self._store.remove_temporal_entry(self.collection_id, stored_block.id)
            after_change(self._hooks, context, change)

    def scan(
        self,
        *,
        occurred_start: datetime | None = None,
        occurred_stop: datetime | None = None,
        valid_at: datetime | None = None,
        overlaps: ValidityInterval | None = None,
        limit: int = 100,
    ) -> list[TemporalItem]:
        """Return visible entries that match all given temporal filters.

        - **Event bounds:** `[occurred_start, occurred_stop)`.
        - **`valid_at`:** selects validity intervals that contain one instant.
        - **`overlaps`:** selects validity intervals that overlap the given
          half-open period.
        - **No time field:** an entry without a time field that a filter
          needs does not match.

        Args:
            occurred_start: Optional inclusive event-time lower bound.
            occurred_stop: Optional exclusive event-time upper bound.
            valid_at: Optional instant that a validity period contains.
            overlaps: Optional period that a validity period overlaps.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in stable ascending block-id order. The list
            leaves out targets that do not exist or that the principal cannot
            read.

        Raises:
            TypeError: If a time bound is not a datetime, `overlaps` is not a
                `ValidityInterval`, or `limit` is not an integer.
            ValueError: If a time bound is naive, `occurred_stop` is earlier
                than `occurred_start`, or `limit` is negative.
        """

        return self._scan(
            occurred_only=False,
            occurred_start=occurred_start,
            occurred_stop=occurred_stop,
            valid_at=valid_at,
            overlaps=overlaps,
            limit=limit,
        )

    def events(
        self,
        *,
        start: datetime | None = None,
        stop: datetime | None = None,
        limit: int = 100,
    ) -> list[TemporalItem]:
        """Return visible entries with event times in a half-open range.

        Entries without an event time are not in the result.

        Args:
            start: Optional inclusive lower bound on event time.
            stop: Optional exclusive upper bound on event time.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. The list leaves
            out targets that do not exist or that the principal cannot read.

        Raises:
            TypeError: If a bound is not a datetime or `limit` is not an
                integer.
            ValueError: If a bound is naive, `stop` is earlier than `start`,
                or `limit` is negative.
        """

        return self._scan(
            occurred_only=True,
            occurred_start=start,
            occurred_stop=stop,
            valid_at=None,
            overlaps=None,
            limit=limit,
        )

    def valid_at(self, when: datetime, *, limit: int = 100) -> list[TemporalItem]:
        """Return visible entries whose validity contains `when`.

        Validity periods are half-open. So an entry matches when `when` is at
        or after its start and before its end.

        Args:
            when: Instant that the validity period of an entry contains.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. The list leaves
            out targets that do not exist or that the principal cannot read.

        Raises:
            TypeError: If `when` is not a datetime or `limit` is not an
                integer.
            ValueError: If `when` is naive or `limit` is negative.
        """

        return self._scan(
            occurred_only=False,
            occurred_start=None,
            occurred_stop=None,
            valid_at=when,
            overlaps=None,
            limit=limit,
        )

    def overlapping(
        self,
        interval: ValidityInterval,
        *,
        limit: int = 100,
    ) -> list[TemporalItem]:
        """Return visible entries whose validity overlaps `interval`.

        Args:
            interval: Validity period that the period of an entry overlaps.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. The list leaves
            out targets that do not exist or that the principal cannot read.

        Raises:
            TypeError: If `interval` is not a `ValidityInterval` or `limit` is
                not an integer.
            ValueError: If `limit` is negative.
        """

        return self._scan(
            occurred_only=False,
            occurred_start=None,
            occurred_stop=None,
            valid_at=None,
            overlaps=interval,
            limit=limit,
        )

    def _scan(
        self,
        *,
        occurred_only: bool,
        occurred_start: datetime | None,
        occurred_stop: datetime | None,
        valid_at: datetime | None,
        overlaps: ValidityInterval | None,
        limit: int,
    ) -> list[TemporalItem]:
        """Scan the store or query the index, then apply the filters and the normal visibility checks."""

        self._require_collection()
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
        if limit == 0:
            return []

        items: list[TemporalItem] = []
        after_block_id: str | None = None
        while len(items) < limit:
            batch_limit = min(100, limit - len(items))
            if self._index is None:
                entries = self._store.list_temporal_entries(
                    self.collection_id,
                    after_block_id=after_block_id,
                    limit=batch_limit,
                )
            else:
                entries = self._index.query_temporal_entries(
                    self.collection_id,
                    occurred_only=occurred_only,
                    occurred_start=occurred_start,
                    occurred_stop=occurred_stop,
                    valid_at=valid_at,
                    overlaps=overlaps,
                    after_block_id=after_block_id,
                    limit=batch_limit,
                )
            if not entries:
                break
            after_block_id = entries[-1].block_id
            for entry in entries:
                if not _matches(
                    entry,
                    occurred_only=occurred_only,
                    occurred_start=occurred_start,
                    occurred_stop=occurred_stop,
                    valid_at=valid_at,
                    overlaps=overlaps,
                ):
                    continue
                try:
                    block = self._access.get(entry.block_id)
                except AccessDenied:
                    continue
                if block is None:
                    continue
                items.append(TemporalItem(entry=entry, block=block))
                if len(items) >= limit:
                    break
            if len(entries) < batch_limit:
                break
        return items

    def _require_collection(self) -> MemoryBlock:
        """Return the visible collection block, or raise `MemoryNotFound` if it was deleted."""

        collection = self._access.get(self.collection_id)
        if collection is None:
            raise MemoryNotFound(f"Temporal collection block {self.collection_id!r} does not exist.")
        return collection

    def _require_writable_collection(self) -> MemoryBlock:
        """Return the visible collection block after a `write` check on it."""

        collection = self._require_collection()
        self._access.require_access(Operation.WRITE, collection.id)
        return collection

    def _require_block(self, block: BlockRef) -> MemoryBlock:
        """Return a visible block, or raise `MemoryNotFound` with a clear message."""

        stored = self._access.get(block)
        if stored is None:
            raise MemoryNotFound(f"Memory block {_block_id(block)!r} does not exist.")
        return stored


def _matches(
    entry: TemporalEntry,
    *,
    occurred_only: bool,
    occurred_start: datetime | None,
    occurred_stop: datetime | None,
    valid_at: datetime | None,
    overlaps: ValidityInterval | None,
) -> bool:
    """Return whether a temporal entry matches all given filters."""

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
    return not (overlaps is not None and (validity is None or not _intervals_overlap(validity, overlaps)))


def _intervals_overlap(left: ValidityInterval, right: ValidityInterval) -> bool:
    """Return whether 2 half-open intervals overlap. An interval can have no end."""

    return not ((left.invalid_at is not None and left.invalid_at <= right.valid_at) or (right.invalid_at is not None and right.invalid_at <= left.valid_at))


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Check an optional timezone-aware datetime, and return it in UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block


def _validate_limit(limit: object) -> None:
    """Raise an error if a result limit is not a non-negative integer."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
