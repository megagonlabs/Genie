"""The change history Mem0 keeps beside its memories.

Mem0 runs a second store purely for provenance.

- Every write the pipeline applies appends a row saying which memory changed,
  what it said before, what it says now, which of `ADD`, `UPDATE`, or `DELETE`
  happened, and who the turn came from.
- `Memory.history(memory_id)` reads the rows for one memory.
- A decision of `NONE` writes nothing, so the history records changes rather
  than attempts.

Two Genie facades over the same event blocks give both readings:

- `VersionedRecords`, anchored on one identity per memory, is that memory's own
  history in order.
- `EventLog`, anchored on the tier, is the journal across every memory in the
  order changes were applied.
- The events are owned by the history tier rather than by the memory they
  describe, which is the point: a deleted memory still has a readable history,
  and its final row is the deletion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from examples.lamps.mem0.model import (
    EVENT_TYPE,
    HISTORY_NAMESPACE,
    READ_LIMIT,
    RECORD_TYPE,
    HistoryEntry,
    MemoryEvent,
    format_timestamp,
    normalize_timestamp,
    optional_text,
    parse_timestamp,
    required_text,
)
from examples.lamps.mem0.structures import Mem0Structures
from genie import MemoryBlock, MemoryHandle
from genie.structures import MemoryAddress


class HistoryLog:
    """Append-only history of every change made to every memory."""

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create a history log over one bounded handle.

        Args:
            memory: Bounded Genie handle owning the history tier.
            structures: Facade bundle built over the same handle.
        """

        self.memory = memory
        self.structures = structures

    def record(
        self,
        memory_id: str,
        event: MemoryEvent,
        *,
        old_memory: str | None = None,
        new_memory: str | None = None,
        actor_id: str | None = None,
        role: str | None = None,
        at: datetime,
    ) -> HistoryEntry:
        """Append one change to a memory's history and to the journal.

        The identity a memory's history hangs from is registered on its first
        event, so a memory that is never written has no history record at all.

        Args:
            memory_id: Memory the change applies to.
            event: `ADD`, `UPDATE`, or `DELETE`.
            old_memory: Text before the change, absent for an `ADD`.
            new_memory: Text after the change, absent for a `DELETE`.
            actor_id: Who the change came from, when a turn named a speaker.
            role: Role of that turn.
            at: When the change was applied.

        Returns:
            The recorded event, carrying both its revision and its journal
            position.

        Raises:
            ValueError: If the event is not a recordable change, or the texts
                do not match the event.
        """

        identifier = required_text(memory_id, field="memory_id")
        applied_at = normalize_timestamp(at, field="at")
        change = _require_change(event, old_memory, new_memory)
        address = MemoryAddress(HISTORY_NAMESPACE, identifier)
        identity = self.structures.history.identity(address)
        if identity is None:
            identity = self.structures.tiers.create_child(
                parent=self.structures.history_root,
                type=RECORD_TYPE,
                value={"memory_id": identifier},
                metadata={"memory_id": identifier},
            )
            self.structures.history.register(address, identity)
        block = self.structures.tiers.create_child(
            parent=identity,
            type=EVENT_TYPE,
            value={
                "memory_id": identifier,
                "event": change,
                "old_memory": old_memory,
                "new_memory": new_memory,
                "actor_id": optional_text(actor_id, field="actor_id"),
                "role": optional_text(role, field="role"),
                "created_at": format_timestamp(applied_at),
                "is_deleted": change == "DELETE",
            },
            metadata={"memory_id": identifier, "event": change},
        )
        revision = self.structures.history.append(address, block)
        entry = self.structures.journal.append(block)
        return _entry(block, revision=revision.entry.revision, sequence=entry.entry.sequence)

    def history(self, memory_id: str, *, limit: int = READ_LIMIT) -> list[HistoryEntry]:
        """Return every recorded change to one memory, oldest first.

        Args:
            memory_id: Memory to read the history of.
            limit: Maximum events to return.

        Returns:
            Events in ascending revision order, empty when the memory was
            never written. A deleted memory keeps its history.
        """

        address = MemoryAddress(HISTORY_NAMESPACE, required_text(memory_id, field="memory_id"))
        return [
            _entry(revision.block, revision=revision.entry.revision)
            for revision in self.structures.history.history(address, limit=limit)
        ]

    def journal(self, *, limit: int = READ_LIMIT) -> list[HistoryEntry]:
        """Return every recorded change to any memory, in applied order.

        Args:
            limit: Maximum events to return.

        Returns:
            Events in ascending journal order.
        """

        return [_entry(item.block, sequence=item.entry.sequence) for item in self.structures.journal.read(limit=limit)]

    def recorded_ids(self, *, limit: int = READ_LIMIT) -> list[str]:
        """Return the ids of every memory that has a history.

        Args:
            limit: Maximum identities to return.

        Returns:
            Memory ids in ascending address order, including ids whose memory
            has since been deleted.
        """

        found = self.structures.history.identities(namespace=HISTORY_NAMESPACE, limit=limit)
        return [entry.binding.address.key for entry in found]


def _entry(block: MemoryBlock, *, revision: int | None = None, sequence: int | None = None) -> HistoryEntry:
    """Decode one recorded change.

    Args:
        block: Block holding a recorded event.
        revision: Position in the memory's own history, when known.
        sequence: Position in the store-wide journal, when known.

    Returns:
        The decoded event.

    Raises:
        TypeError: If the block is not a recorded history event.
    """

    if block.type != EVENT_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a Mem0 history event.")
    payload = cast(dict[str, Any], block.value)
    return HistoryEntry(
        block=block,
        memory_id=required_text(payload.get("memory_id"), field="memory_id"),
        event=cast(MemoryEvent, required_text(payload.get("event"), field="event")),
        old_memory=cast("str | None", payload.get("old_memory")),
        new_memory=cast("str | None", payload.get("new_memory")),
        actor_id=cast("str | None", payload.get("actor_id")),
        role=cast("str | None", payload.get("role")),
        created_at=parse_timestamp(required_text(payload.get("created_at"), field="created_at")),
        is_deleted=bool(payload.get("is_deleted")),
        revision=revision,
        sequence=sequence,
    )


def _require_change(event: str, old_memory: str | None, new_memory: str | None) -> MemoryEvent:
    """Validate that an event and its texts describe one another.

    Args:
        event: Event name supplied by the caller.
        old_memory: Text before the change.
        new_memory: Text after the change.

    Returns:
        The validated event name.

    Raises:
        ValueError: If the event is not recordable, or the texts do not match
            it.
    """

    if event not in {"ADD", "UPDATE", "DELETE"}:
        raise ValueError("Only ADD, UPDATE, and DELETE are recorded in the history.")
    if event == "ADD" and (old_memory is not None or new_memory is None):
        raise ValueError("An ADD records only the new memory.")
    if event == "UPDATE" and (old_memory is None or new_memory is None):
        raise ValueError("An UPDATE records both the old and the new memory.")
    if event == "DELETE" and (old_memory is None or new_memory is not None):
        raise ValueError("A DELETE records only the old memory.")
    return cast(MemoryEvent, event)
