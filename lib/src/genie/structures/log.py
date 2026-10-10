"""Ordered event-log facade over an atomic sequence store."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genie.backend.base import BlockRouter
from genie.backend.structure_capabilities import SequenceStore
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
from genie.structures.records import LogEntry, LogItem


@runtime_checkable
class SequenceAccess(BlockReader, AccessChecker, Protocol):
    """Small access surface required by `EventLog`."""


class EventLog:
    """An append-only ordered view over existing memory blocks.

    The sequence store atomically allocates stable positions. Blocks remain
    owned and stored through normal Genie memory and may appear in more than one
    log or structure.

    Reading needs `read` on the log block and on each member. `append` also
    needs `write` on the log block, checked through the access object, so a
    principal that can only read the log cannot add to it.
    """

    __slots__ = (
        "_access",
        "_attributes",
        "_hooks",
        "_principal_id",
        "_store",
        "log_id",
    )

    def __init__(
        self,
        access: SequenceAccess,
        store: SequenceStore | BlockRouter,
        *,
        log: BlockRef,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Create an event-log view anchored by a visible log block.

        Args:
            access: Full or scoped memory access used to resolve blocks.
            store: Backend capability that appends entries atomically, or a
                routing backend that resolves to the route holding the log.
            log: Block or id that identifies this ordered log.
            hooks: Optional registry. Defaults to the registry owned by a
                `Memory` or `MemoryHandle` access object.

        Raises:
            MemoryNotFound: If the log block is missing.
            UnsupportedOperation: If `store` lacks the sequence capability.
        """

        store = resolve_store(store, log, missing_message=f"Event log block {_block_id(log)!r} does not exist.")
        if not isinstance(store, SequenceStore):
            raise UnsupportedOperation("The configured store does not support event logs.")
        stored_log = access.get(log)
        if stored_log is None:
            raise MemoryNotFound(f"Event log block {_block_id(log)!r} does not exist.")
        self._access = access
        self._store = store
        self._hooks = resolve_hooks(access, hooks)
        self._principal_id = principal_id(access)
        self._attributes = context_attributes(access)
        self.log_id = stored_log.id

    @property
    def log(self) -> MemoryBlock:
        """Return the current visible log block."""

        return self._require_log()

    def append(self, block: BlockRef) -> LogItem:
        """Atomically append an existing visible block to this log.

        Args:
            block: Existing memory block or id.

        Returns:
            Allocated log entry and resolved block.

        Raises:
            AccessDenied: If the log denies `write` or the block denies `read`.
            MemoryNotFound: If the log or block is missing.
        """

        with hook_operation(
            self._hooks,
            HookOperation.LOG_APPEND,
            principal=self._principal_id,
            attributes=self._attributes,
        ) as context:
            self._require_writable_log()
            stored_block = self._require_block(block)
            change = StructureChange[LogEntry](
                kind=StructureKind.EVENT_LOG,
                action=StructureAction.APPEND,
                container_id=self.log_id,
                block_id=stored_block.id,
            )
            before_change(self._hooks, context, change)
            entry = self._store.append_log_entry(self.log_id, stored_block.id)
            after_change(
                self._hooks,
                context,
                StructureChange(
                    kind=change.kind,
                    action=change.action,
                    container_id=change.container_id,
                    block_id=change.block_id,
                    after=entry,
                ),
            )
            return LogItem(entry=entry, block=stored_block)

    def get(self, sequence: int) -> LogItem | None:
        """Return the visible item at one exact sequence position.

        Args:
            sequence: Exact position within the log.

        Returns:
            Visible log item, or `None` when the position holds no entry.

        Raises:
            MemoryNotFound: If the position references a block that no longer
                exists.
            TypeError: If `sequence` is not an integer.
            ValueError: If `sequence` is negative.
        """

        self._require_log()
        _validate_sequence(sequence, name="sequence")
        entry = self._store.get_log_entry(self.log_id, sequence)
        if entry is None:
            return None
        return self._resolve_exact(entry)

    def read(
        self,
        *,
        start: int | None = None,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[LogItem]:
        """Read visible entries from an optional half-open sequence range.

        Args:
            start: Optional inclusive starting sequence.
            stop: Optional exclusive ending sequence.
            reverse: Whether to return newest entries first.
            limit: Maximum visible items to return.

        Returns:
            Visible log items in ascending or descending sequence order.
            Missing or inaccessible targets are omitted.

        Raises:
            TypeError: If `reverse` is not a boolean, or `start`, `stop`, or
                `limit` is not an integer.
            ValueError: If a bound or `limit` is negative, or `stop` is less
                than `start`.
        """

        self._require_log()
        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean.")
        if start is not None:
            _validate_sequence(start, name="start")
        if stop is not None:
            _validate_sequence(stop, name="stop")
        if start is not None and stop is not None and stop < start:
            raise ValueError("stop must be greater than or equal to start.")
        _validate_limit(limit)
        if limit == 0:
            return []

        items: list[LogItem] = []
        cursor_start = start
        cursor_stop = stop
        while len(items) < limit:
            batch_limit = min(100, limit - len(items))
            entries = self._store.list_log_entries(
                self.log_id,
                start=cursor_start,
                stop=cursor_stop,
                reverse=reverse,
                limit=batch_limit,
            )
            if not entries:
                break
            if reverse:
                cursor_stop = entries[-1].sequence
            else:
                cursor_start = entries[-1].sequence + 1
            for entry in entries:
                try:
                    block = self._access.get(entry.block_id)
                except AccessDenied:
                    continue
                if block is None:
                    continue
                items.append(LogItem(entry=entry, block=block))
                if len(items) >= limit:
                    break
            if len(entries) < batch_limit:
                break
        return items

    def tail(self, *, limit: int = 20) -> list[LogItem]:
        """Return the newest visible items in chronological order.

        Args:
            limit: Maximum visible items to return.

        Returns:
            Up to `limit` of the newest visible items, oldest first.

        Raises:
            TypeError: If `limit` is not an integer.
            ValueError: If `limit` is negative.
        """

        items = self.read(reverse=True, limit=limit)
        items.reverse()
        return items

    def _resolve_exact(self, entry: LogEntry) -> LogItem:
        """Resolve one exact entry without hiding access or integrity errors."""

        block = self._access.get(entry.block_id)
        if block is None:
            raise MemoryNotFound(
                f"Log entry {entry.sequence} in {entry.log_id!r} references "
                f"missing memory block {entry.block_id!r}."
            )
        return LogItem(entry=entry, block=block)

    def _require_log(self) -> MemoryBlock:
        """Return the visible log block or raise when it was deleted."""

        log = self._access.get(self.log_id)
        if log is None:
            raise MemoryNotFound(f"Event log block {self.log_id!r} does not exist.")
        return log

    def _require_writable_log(self) -> MemoryBlock:
        """Return the visible log block after requiring `write` on it."""

        log = self._require_log()
        self._access.require_access(Operation.WRITE, log.id)
        return log

    def _require_block(self, block: BlockRef) -> MemoryBlock:
        """Return a visible block or raise a clear missing-block error."""

        stored = self._access.get(block)
        if stored is None:
            raise MemoryNotFound(f"Memory block {_block_id(block)!r} does not exist.")
        return stored


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block


def _validate_sequence(value: object, *, name: str) -> None:
    """Require a non-negative integer sequence value."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_limit(limit: object) -> None:
    """Require a non-negative integer result limit."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
