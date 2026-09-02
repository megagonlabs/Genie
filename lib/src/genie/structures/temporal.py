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
from genie.policy.engine import AccessDenied
from genie.structures._hooks import (
    after_change,
    before_change,
    context_attributes,
    hook_operation,
    principal_id,
    resolve_hooks,
)
from genie.structures._routing import resolve_store
from genie.structures.access import BlockReader, BlockRef
from genie.structures.records import TemporalEntry, TemporalItem, ValidityInterval


@runtime_checkable
class TemporalAccess(BlockReader, Protocol):
    """Small access surface required by `TemporalCollection`."""


class TemporalCollection:
    """A time-filtered view over existing memory blocks.

    Temporal membership is separate from block ownership and values. Every
    collection can attach one event time, validity period, or both to a block.
    Queries scan the portable store unless a compatible temporal index is
    supplied. Both paths resolve results through normal memory access so scope
    and block policies remain in effect.
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
        """Create a temporal view anchored by a visible collection block.

        Args:
            access: Full or scoped memory access used to resolve blocks.
            store: Backend capability that persists temporal membership, or a
                routing backend that resolves to the route holding the
                collection.
            collection: Block or id that identifies this temporal collection.
            index: Optional efficient temporal query index. When omitted, the
                store is used as the index if it implements `TemporalIndex`;
                otherwise queries use a portable paged scan. A routing backend
                resolves to the route holding the collection.
            hooks: Optional registry. Defaults to the registry owned by a
                `Memory` or `MemoryHandle` access object.

        Raises:
            MemoryNotFound: If the collection block is missing.
            UnsupportedOperation: If a supplied capability is unsupported.
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
            validity: Optional half-open period when information is valid.

        Returns:
            Created temporal entry and resolved block.

        Raises:
            MemoryConflict: If the block is already in this collection.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_ADD,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_collection()
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
            occurred_at: Replacement event time, or `None` to omit it.
            validity: Replacement validity period, or `None` to omit it.

        Returns:
            Replacement temporal entry and resolved block.

        Raises:
            MemoryConflict: If the block is not in this collection.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_REPLACE,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_collection()
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
            Temporal item pairing the entry with its block, or `None` when the
            collection does not contain the block.

        Raises:
            MemoryNotFound: If the entry references a block that no longer
                exists.
        """

        self._require_collection()
        entry = self._store.get_temporal_entry(self.collection_id, _block_id(block))
        if entry is None:
            return None
        return self._resolve_exact(entry)

    def remove(self, block: BlockRef) -> None:
        """Remove temporal membership without deleting the memory block.

        Args:
            block: Existing visible temporal member.

        Raises:
            MemoryConflict: If the block is not in this collection.
        """

        with hook_operation(
            self._hooks,
            HookOperation.TEMPORAL_REMOVE,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_collection()
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
        """Return visible entries matching all supplied temporal filters.

        Event bounds use `[occurred_start, occurred_stop)`. `valid_at`
        selects validity intervals containing one instant. `overlaps` selects
        validity intervals that overlap the supplied half-open period. Entries
        missing a time field required by a filter do not match.

        Args:
            occurred_start: Optional inclusive event-time lower bound.
            occurred_stop: Optional exclusive event-time upper bound.
            valid_at: Optional instant that a validity period must contain.
            overlaps: Optional period that a validity period must overlap.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in stable ascending block-id order.
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

        Entries without an event time are excluded.

        Args:
            start: Optional inclusive lower bound on event time.
            stop: Optional exclusive upper bound on event time.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. Missing or
            inaccessible targets are omitted.

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

        Validity periods are half-open, so an entry matches when `when` is at
        or after its start and strictly before its end.

        Args:
            when: Instant an entry's validity period must contain.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. Missing or
            inaccessible targets are omitted.

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
            interval: Validity period an entry's own period must overlap.
            limit: Maximum visible items to return.

        Returns:
            Visible temporal items in ascending block-id order. Missing or
            inaccessible targets are omitted.

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
        """Scan or index temporal entries and apply normal visibility checks."""

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

    def _resolve_exact(self, entry: TemporalEntry) -> TemporalItem:
        """Resolve one exact entry without hiding access or integrity errors."""

        block = self._access.get(entry.block_id)
        if block is None:
            raise MemoryNotFound(f"Temporal entry in {entry.collection_id!r} references missing " f"memory block {entry.block_id!r}.")
        return TemporalItem(entry=entry, block=block)

    def _require_collection(self) -> MemoryBlock:
        """Return the visible collection block or raise when it was deleted."""

        collection = self._access.get(self.collection_id)
        if collection is None:
            raise MemoryNotFound(f"Temporal collection block {self.collection_id!r} does not exist.")
        return collection

    def _require_block(self, block: BlockRef) -> MemoryBlock:
        """Return a visible block or raise a clear missing-block error."""

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
    """Return whether a temporal entry satisfies all supplied filters."""

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
    """Return whether two half-open, optionally open-ended intervals overlap."""

    return not ((left.invalid_at is not None and left.invalid_at <= right.valid_at) or (right.invalid_at is not None and right.invalid_at <= left.valid_at))


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Validate and normalize an optional aware datetime to UTC."""

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
    """Require a non-negative integer result limit."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
