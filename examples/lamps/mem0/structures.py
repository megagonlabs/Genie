"""Genie facades for the 3 ownership tiers of the Mem0 lamp.

Mem0 uses 3 stores that are independent of each other:

- **Vector store:** holds one payload for each memory. The payload scope is the
  subset of `user_id`, `agent_id`, and `run_id` that the caller supplied.
- **Relational history table:** holds one row for each change to a memory.
- **Graph store (optional):** holds entities and the relationships between them.

The 3 tiers below keep that split. Thus 2 paths reach the same memory:

- **Id:** the id is a `KeyedCollection` address, because `get`, `update`, and
  `delete` take an id and no scope.
- **Session:** the session is an ownership path, because a search takes a scope.
  A filter on the supplied ids selects the same blocks as a subtree scope.
  Ownership also tells what happens to those memories when the application
  retires the user.

The history tier has 2 facades over the same event blocks:

- **`EventLog`:** its anchor is the tier itself. It is the journal in arrival
  order, that is, the full history table.
- **`VersionedRecords`:** its anchor is one identity for each memory. It is the
  history of that memory, which `Memory.history(memory_id)` returns.
- **Both survive a delete:** both facades continue to work after the lamp
  deletes the memory. This tier owns the events, not the memory that they
  describe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from examples.lamps.mem0.model import ANCHOR_TYPE, TIERS
from genie import MemoryBlock, MemoryHandle
from genie.backend import KeyedStore, SequenceStore
from genie.structures import (
    EventLog,
    KeyedCollection,
    OwnershipTree,
    PropertyGraph,
    VersionedRecords,
)


class Mem0StructureStore(KeyedStore, SequenceStore, Protocol):
    """Backend capabilities that the Mem0 structure bundle requires."""


@dataclass(frozen=True, slots=True)
class Mem0Structures:
    """The structure views that this lamp composes over one memory store.

    Attributes:
        tiers: Ownership view of the 3 tiers below the store root.
        records: Map from memory ids to the blocks that hold the memories.
        history: One identity for each memory, and one revision for each
            recorded change.
        journal: All recorded changes, in the order that the lamp applied them.
        entities: Map from graph entity names to their nodes. There is one
            namespace for each outermost session identifier.
        graph: Typed relationships between those nodes.
        memory_root: Block that owns the session paths and the memories that
            have addresses in them. The session paths copy the session
            identifiers.
        history_root: Block that owns the history identities and their events.
        entity_root: Block that owns the graph entities.
    """

    tiers: OwnershipTree
    records: KeyedCollection
    history: VersionedRecords
    journal: EventLog
    entities: KeyedCollection
    graph: PropertyGraph
    memory_root: MemoryBlock
    history_root: MemoryBlock
    entity_root: MemoryBlock

    @classmethod
    def create(cls, memory: MemoryHandle, store: Mem0StructureStore) -> Mem0Structures:
        """Build or reopen all structure anchors below one bounded root.

        The method finds the anchors that exist already in the children of the
        root. Thus a reopened store returns the same structures and does not
        make duplicates.

        Args:
            memory: Handle with its root at the block that owns the whole store.
            store: Backend with the keyed and sequence capabilities.

        Returns:
            The structure bundle for that store.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("Mem0Structures requires a bounded MemoryHandle.")
        tiers = OwnershipTree(memory, root=memory.root_id)
        anchors = {name: _anchor(tiers, name) for name in TIERS}
        return cls(
            tiers=tiers,
            records=KeyedCollection(memory, store, collection=anchors["memories"]),
            history=VersionedRecords(memory, store, collection=anchors["history"]),
            journal=EventLog(memory, store, log=anchors["history"]),
            entities=KeyedCollection(memory, store, collection=anchors["entities"]),
            graph=PropertyGraph(memory),
            memory_root=anchors["memories"],
            history_root=anchors["history"],
            entity_root=anchors["entities"],
        )


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, and create it if it does not exist.

    The function matches anchors on metadata, not on value. Thus it can find a
    tier again, also if that tier keeps state in its own value.
    """

    existing = next(
        (child for child in tiers.children() if child.type == ANCHOR_TYPE and child.metadata.get("tier") == name),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(type=ANCHOR_TYPE, value={"tier": name}, metadata={"tier": name})
