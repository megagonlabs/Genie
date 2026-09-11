"""Genie facades wired to the Mem0 lamp's three ownership tiers.

Mem0 runs on stores that know nothing about each other:

- A vector store holds one payload per memory, scoped by whichever of
  `user_id`, `agent_id`, and `run_id` the caller supplied.
- A relational history table holds one row per change to any of them.
- An optional graph store holds entities and the relationships between them.

That split is what the three tiers below preserve, and it is why the same memory
is reachable two ways:

- Its id is a `KeyedCollection` address, because `get`, `update`, and `delete`
  take an id and no scope.
- Its session is an ownership path, because a search does take a scope. "Filter
  on the ids I supplied" selects exactly the blocks a subtree scope does, with
  the difference that ownership also answers what happens to those memories when
  the user is retired.

The history tier carries two facades over the same event blocks:

- `EventLog`, anchored on the tier itself, is the journal in arrival order,
  which is the history table read whole.
- `VersionedRecords`, anchored on one identity per memory, is that memory's own
  history, which is what `Memory.history(memory_id)` returns.
- Both keep working after the memory is deleted, because the events are owned by
  this tier rather than by the memory they describe.
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
    """Backend capabilities required by the Mem0 structure bundle."""


@dataclass(frozen=True, slots=True)
class Mem0Structures:
    """The structural views this lamp composes over one memory store.

    Attributes:
        tiers: Ownership view of the three tiers below the store root.
        records: Memory ids mapped to the blocks holding them.
        history: One identity per memory, one revision per recorded change.
        journal: Every recorded change in the order it was applied.
        entities: Graph entity names mapped to their nodes, per session path.
        graph: Typed relationships between those nodes.
        memory_root: Block that owns the mirrored session paths and the
            memories addressed inside them.
        history_root: Block that owns history identities and their events.
        entity_root: Block that owns graph entities.
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
        """Build or reopen every structural anchor below one bounded root.

        Existing anchors are found by scanning the root's children, so
        reopening a store returns the same structures instead of duplicating
        them.

        Args:
            memory: Handle rooted at the block that owns the whole store.
            store: Backend providing keyed and sequence capabilities.

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
    """Return the tier anchor with this name, creating it when absent.

    Anchors are matched on metadata rather than on value so that a tier which
    keeps state in its own value can still be found again.
    """

    existing = next(
        (child for child in tiers.children() if child.type == ANCHOR_TYPE and child.metadata.get("tier") == name),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(type=ANCHOR_TYPE, value={"tier": name}, metadata={"tier": name})
