"""The change history that Mem0 keeps with its memories.

Mem0 has a second store only for provenance, that is, the source of each memory:

- **One row for each write:** each write that the pipeline applies adds a row.
  The row tells which memory changed, its old text, and its new text. It also
  tells which of `ADD`, `UPDATE`, or `DELETE` occurred, and the speaker of the
  source turn.
- **One memory:** `Memory.history(memory_id)` reads the rows for one memory.
- **No row for `NONE`:** a `NONE` decision writes nothing. Thus the history
  records changes, not attempts.

2 Genie facades over the same event blocks give the 2 views:

- **`VersionedRecords`:** its anchor is one identity for each memory. It is the
  history of that memory, in order.
- **`EventLog`:** its anchor is the tier. It is the journal of all memories, in
  the order that the lamp applied the changes.
- **The history tier owns the events:** the memory that the events describe does
  not own them. This is the purpose of the split. A deleted memory still has a
  history that a caller can read, and the last row is the delete.
- **Scope on the identity:** each identity also keeps the scope path of its
  memory. The events continue to exist after the delete of the memory, but then
  the store cannot tell who owned the memory. Thus a caller that checks the
  scope of a history read uses the scope on the identity.
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
    Session,
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
    """Append-only history of all changes to all memories."""

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create a history log over one bounded handle.

        Args:
            memory: Bounded Genie handle that owns the history tier.
            structures: Facade bundle over the same handle.
        """

        self.memory = memory
        self.structures = structures

    def record(
        self,
        memory_id: str,
        event: MemoryEvent,
        *,
        session: Session,
        old_memory: str | None = None,
        new_memory: str | None = None,
        actor_id: str | None = None,
        role: str | None = None,
        at: datetime,
    ) -> HistoryEntry:
        """Append one change to the history of a memory and to the journal.

        - **Identity at the first event:** the method registers the identity of
          a memory history at the first event of that memory. Thus a memory
          with no writes has no history record.
        - **Fixed scope:** the method writes the scope on that identity once,
          and does not change it later. A memory stays in the scope where the
          lamp created it.

        Args:
            memory_id: Memory that the change applies to.
            event: `ADD`, `UPDATE`, or `DELETE`.
            session: Scope of the memory. The identity keeps it. Thus the
                history still shows the owner scope after the delete of the
                memory.
            old_memory: Text before the change. `None` for an `ADD`.
            new_memory: Text after the change. `None` for a `DELETE`.
            actor_id: Speaker that the change came from, if a turn named a
                speaker.
            role: Role of that turn.
            at: Time when the lamp applied the change.

        Returns:
            The recorded event, with its revision and its journal position.

        Raises:
            ValueError: If the history does not record this event type, or if
                the texts do not agree with the event.
        """

        identifier = required_text(memory_id, field="memory_id")
        applied_at = normalize_timestamp(at, field="at")
        change = _require_change(event, old_memory, new_memory)
        address = MemoryAddress(HISTORY_NAMESPACE, identifier)
        identity = self.structures.history.identity(address)
        if identity is None:
            scope = list(session.path)
            identity = self.structures.tiers.create_child(
                parent=self.structures.history_root,
                type=RECORD_TYPE,
                value={"memory_id": identifier, "scope": scope},
                metadata={"memory_id": identifier, "scope": "/".join(scope)},
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
        """Return all recorded changes to one memory, the oldest first.

        Args:
            memory_id: Memory to read the history of.
            limit: Maximum number of events to return.

        Returns:
            Events in ascending revision order. The list is empty if the memory
            had no writes. A deleted memory keeps its history.
        """

        address = MemoryAddress(HISTORY_NAMESPACE, required_text(memory_id, field="memory_id"))
        return [
            _entry(revision.block, revision=revision.entry.revision)
            for revision in self.structures.history.history(address, limit=limit)
        ]

    def owner(self, memory_id: str) -> tuple[str, ...] | None:
        """Return the scope path of the memory that one history belongs to.

        Args:
            memory_id: Memory for which to find the owner scope.

        Returns:
            The recorded path, or `None` if the log has no history for that id.
            A deleted memory still gives its path, because its identity
            continues to exist after the delete.
        """

        address = MemoryAddress(HISTORY_NAMESPACE, required_text(memory_id, field="memory_id"))
        identity = self.structures.history.identity(address)
        if identity is None or not isinstance(identity.value, dict):
            return None
        recorded = cast(dict[str, Any], identity.value).get("scope")
        if not isinstance(recorded, list):
            return None
        return tuple(str(segment) for segment in recorded)

    def journal(self, *, limit: int = READ_LIMIT) -> list[HistoryEntry]:
        """Return all recorded changes to all memories, in the order that the lamp applied them.

        Args:
            limit: Maximum number of events to return.

        Returns:
            Events in ascending journal order.
        """

        return [_entry(item.block, sequence=item.entry.sequence) for item in self.structures.journal.read(limit=limit)]

    def recorded_ids(self, *, limit: int = READ_LIMIT) -> list[str]:
        """Return the ids of all memories that have a history.

        Args:
            limit: Maximum number of identities to return.

        Returns:
            Memory ids in ascending address order. The list includes the ids of
            deleted memories.
        """

        found = self.structures.history.identities(namespace=HISTORY_NAMESPACE, limit=limit)
        return [entry.binding.address.key for entry in found]


def _entry(block: MemoryBlock, *, revision: int | None = None, sequence: int | None = None) -> HistoryEntry:
    """Decode one recorded change.

    Args:
        block: Block that holds a recorded event.
        revision: Position in the history of the memory, if known.
        sequence: Position in the store-wide journal, if known.

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
    """Validate that an event and its texts agree.

    Args:
        event: Event name from the caller.
        old_memory: Text before the change.
        new_memory: Text after the change.

    Returns:
        The validated event name.

    Raises:
        ValueError: If the history does not record this event type, or if the
            texts do not agree with it.
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
