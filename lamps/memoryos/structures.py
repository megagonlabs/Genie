"""Genie facades wired to the MemoryOS lamp's four tiers.

MemoryOS runs on four stores that are written to and evicted from on different
rules: a short-term FIFO queue, a mid-term store of topic segments, and one
long-term store each for what the user knows and what the assistant knows. The
tier anchors below keep that separation, because the interesting behavior of a
memory operating system is what happens at the boundaries between them.

Each tier gets the structure that matches how it is read:

| Tier | Facade | Why that one |
| --- | --- | --- |
| short-term | `EventLog` | The queue *is* arrival order, and overflow reads the oldest end |
| mid-term | `OwnershipTree` | A segment owns its pages, so dropping it drops them |
| mid-term | `TemporalCollection` | Last-visit times, which the recency term reads |
| user long-term | `VersionedRecords` | The profile keeps every version an analysis wrote |
| user long-term | `EventLog` | A capped knowledge base, evicted oldest first |
| assistant long-term | `EventLog` | The same, for what the assistant learned |

Two facades cut across the tiers. A `KeyedCollection` holds the page and session
id spaces, so `get_page_by_id` is a lookup rather than a walk over every session.
And a `LineageDAG` on `memoryos.next_page` holds the dialogue chain, which is the
one relationship in MemoryOS that follows the dialogue rather than the storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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
from lamps.memoryos.model import NEXT_PAGE, TIER_TYPE, TIERS


class MemoryOSStore(AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore, Protocol):
    """Backend capabilities required by the MemoryOS structure bundle."""


@dataclass(frozen=True, slots=True)
class MemoryOSStructures:
    """The structural views this lamp composes over one user's memory.

    Attributes:
        tiers: Ownership view of the four tiers below the system root.
        queue: Short-term memory in arrival order.
        segments: Mid-term sessions, each owning its pages.
        visits: Last-visit times for those sessions.
        ids: Page and session id spaces in one keyed collection.
        chain: The dialogue chain, as cycle-protected lineage edges.
        profile: The user profile, one revision per analysis.
        user_knowledge: The user's capped knowledge base.
        assistant_knowledge: The assistant's capped knowledge base.
        provenance: Edges from a promoted fact back to the pages it came from.
        short_term_root: Block that owns the queued exchanges.
        mid_term_root: Block that owns the sessions.
        user_root: Block that owns the profile and the user's knowledge.
        assistant_root: Block that owns the assistant's knowledge.
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
        """Build or reopen every structural anchor below one bounded root.

        Existing anchors are found by scanning the root's children, so
        reopening a user's memory returns the same structures instead of
        duplicating them.

        Args:
            memory: Handle rooted at the block that owns one user's memory.
            store: Backend providing acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that memory.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("MemoryOSStructures requires a bounded MemoryHandle.")
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
    """Return the tier anchor with this name, creating it when absent.

    Anchors are matched on metadata rather than on value so a tier that keeps
    counters in its own value can still be found again.
    """

    existing = next(
        (child for child in tiers.children() if child.type == TIER_TYPE and child.metadata.get("tier") == name),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(type=TIER_TYPE, value={"tier": name}, metadata={"tier": name})
