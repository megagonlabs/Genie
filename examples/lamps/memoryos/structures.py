"""Genie facades for the 4 tiers of the MemoryOS lamp.

MemoryOS uses 4 stores. Each store has its own rules for writes and evictions:

- **Short-term:** a first-in, first-out (FIFO) queue.
- **Mid-term:** a store of topic segments.
- **Long-term:** 2 stores, one for user knowledge and one for assistant
  knowledge.

The tier anchors keep these stores separate. The important behavior of a memory
operating system occurs at the boundaries between the tiers.

Each tier uses the structure that matches how the lamp reads it:

| Tier | Facade | Why that one |
| --- | --- | --- |
| short-term | `EventLog` | The queue is the arrival order, and overflow reads the oldest end |
| mid-term | `OwnershipTree` | A segment owns its pages, so a delete of the segment also deletes its pages |
| mid-term | `TemporalCollection` | Last-visit times, which the recency term reads |
| user long-term | `VersionedRecords` | The profile keeps each version that an analysis wrote |
| user long-term | `EventLog` | A knowledge base with a capacity, which evicts the oldest entry first |
| assistant long-term | `EventLog` | The same, for what the assistant learned |

2 facades cross the tiers:

- **`KeyedCollection`:** holds the page id space and the session id space. So
  `get_page_by_id` is one lookup and not a walk over each session.
- **`LineageDAG` on `memoryos.next_page`:** holds the dialogue chain. This is the
  only relationship in MemoryOS that follows the dialogue and not the storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from examples.lamps.memoryos.model import NEXT_PAGE, TIER_TYPE, TIERS
from genie import MemoryBlock, MemoryHandle
from genie.backend import AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore
from genie.structures import (
    EventLog,
    KeyedCollection,
    LineageDAG,
    OwnershipTree,
    PropertyGraph,
    TemporalCollection,
    VersionedRecords,
)


class MemoryOSStore(AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore, Protocol):
    """Backend capabilities that the MemoryOS structure bundle requires."""


@dataclass(frozen=True, slots=True)
class MemoryOSStructures:
    """The structure views that this lamp builds over the memory of one user.

    Attributes:
        tiers: Ownership view of the 4 tiers below the system root.
        queue: Short-term memory in arrival order.
        segments: Mid-term sessions. Each session owns its pages.
        visits: Last-visit times for those sessions.
        ids: Page id space and session id space in one keyed collection.
        chain: The dialogue chain, as lineage edges that cannot make a cycle.
        profile: The user profile, with 1 revision for each analysis.
        user_knowledge: The knowledge base of the user, with a capacity.
        assistant_knowledge: The knowledge base of the assistant, with a capacity.
        provenance: Edges from a promoted fact to the pages that it came from.
        short_term_root: Block that owns the exchanges in the queue.
        mid_term_root: Block that owns the sessions.
        user_root: Block that owns the profile and the knowledge of the user.
        assistant_root: Block that owns the knowledge of the assistant.
    """

    tiers: OwnershipTree
    queue: EventLog
    segments: OwnershipTree
    visits: TemporalCollection
    ids: KeyedCollection
    chain: LineageDAG
    profile: VersionedRecords
    user_knowledge: EventLog
    assistant_knowledge: EventLog
    provenance: PropertyGraph
    short_term_root: MemoryBlock
    mid_term_root: MemoryBlock
    user_root: MemoryBlock
    assistant_root: MemoryBlock

    @classmethod
    def create(cls, memory: MemoryHandle, store: MemoryOSStore) -> MemoryOSStructures:
        """Build or reopen each structure anchor below one bounded root.

        - **Reopen:** the method finds the current anchors in the children of
          the root. So a second call for the same memory returns the same
          structures and does not make duplicates.
        - **Chain protection:** the method protects `NEXT_PAGE` through
          `store`, because a handle cannot. A repeated call changes nothing.

        Args:
            memory: Handle whose root is the block that owns the memory of one
                user.
            store: Backend with the acyclic-link, keyed, sequence, and temporal
                capabilities.

        Returns:
            The structure bundle for that memory.

        Raises:
            MemoryConflict: If the stored links of `NEXT_PAGE` contain a cycle.
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("MemoryOSStructures requires a bounded MemoryHandle.")
        store.protect_relation(NEXT_PAGE)
        tiers = OwnershipTree(memory, root=memory.root_id)
        anchors = {name: _anchor(tiers, name) for name in TIERS}
        mid_term = anchors["mid_term"]
        return cls(
            tiers=tiers,
            queue=EventLog(memory, store, log=anchors["short_term"]),
            segments=OwnershipTree(memory, root=mid_term),
            visits=TemporalCollection(memory, store, collection=mid_term),
            ids=KeyedCollection(memory, store, collection=mid_term),
            chain=LineageDAG(memory, relation=NEXT_PAGE),
            profile=VersionedRecords(memory, store, collection=anchors["user_long_term"]),
            user_knowledge=EventLog(memory, store, log=anchors["user_long_term"]),
            assistant_knowledge=EventLog(memory, store, log=anchors["assistant_long_term"]),
            provenance=PropertyGraph(memory),
            short_term_root=anchors["short_term"],
            mid_term_root=mid_term,
            user_root=anchors["user_long_term"],
            assistant_root=anchors["assistant_long_term"],
        )


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, and create it if it does not exist.

    The function matches anchors on metadata and not on value. So it can find a
    tier again even if that tier keeps counters in its own value.
    """

    existing = next(
        (child for child in tiers.children() if child.type == TIER_TYPE and child.metadata.get("tier") == name),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(type=TIER_TYPE, value={"tier": name}, metadata={"tier": name})
