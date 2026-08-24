"""Genie facades wired to the LangMem lamp's five ownership tiers.

LangMem stores memories in a namespaced key-value store with vector search.
The namespace is only a name there: nothing owns it, so nothing happens to the
memories inside it when a user goes away. This lamp mirrors every namespace as
an ownership path instead, which buys two things a plain key-value store cannot
give. Prefix search becomes a Genie subtree scope rather than a filter the lamp
applies afterwards, and deleting a namespace deletes the memories addressed
inside it.

Two other wiring choices carry most of the model. Profiles and prompts are both
`VersionedRecords`, because both are single documents that are revised rather
than accumulated, and both must be able to show what they used to say. And the
deferred reflection queue anchors an `EventLog` and a `TemporalCollection` on
the same tier: the log is the order submissions arrived, the temporal entry is
when each one comes due, so LangMem's debounce window becomes a time query and
a cancelled submission stays on the log.
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
    TemporalCollection,
    VersionedRecords,
)
from lamps.langmem.model import ANCHOR_TYPE, DERIVED_FROM_RELATION, TIERS


class LangMemStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities required by the LangMem structure bundle."""


@dataclass(frozen=True, slots=True)
class LangMemStructures:
    """The structural views this lamp composes over one memory store.

    Attributes:
        tiers: Ownership view of the five tiers below the store root.
        items: Namespaced addresses of every stored memory.
        profiles: One identity per profile, one revision per update.
        prompts: One identity per prompt, one revision per optimization.
        queue: Append-only order in which conversations were submitted.
        schedule: When each queued submission comes due.
        lineage: Acyclic derivation edges from conversations to the memories
            they wrote and from trajectories to the prompt versions they
            produced.
        namespaces: Block that owns the mirrored namespace paths.
        trajectories: Block that owns annotated past exchanges.
    """

    tiers: OwnershipTree
    items: KeyedCollection
    profiles: VersionedRecords
    prompts: VersionedRecords
    queue: EventLog
    schedule: TemporalCollection
    lineage: LineageDAG
    namespaces: MemoryBlock
    trajectories: MemoryBlock

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: LangMemStructureStore,
    ) -> LangMemStructures:
        """Build or reopen every structural anchor below one bounded root.

        Existing anchors are found by scanning the root's children, so
        reopening a store returns the same structures instead of duplicating
        them.

        Args:
            memory: Handle rooted at the block that owns the whole store.
            store: Backend providing acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that store.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("LangMemStructures requires a bounded MemoryHandle.")
        tiers = OwnershipTree(memory, root=memory.root_id)
        anchors = {name: _anchor(tiers, name) for name in TIERS}
        return cls(
            tiers=tiers,
            items=KeyedCollection(memory, store, collection=anchors["namespaces"]),
            profiles=VersionedRecords(memory, store, collection=anchors["profiles"]),
            prompts=VersionedRecords(memory, store, collection=anchors["prompts"]),
            queue=EventLog(memory, store, log=anchors["queue"]),
            schedule=TemporalCollection(memory, store, collection=anchors["queue"]),
            lineage=LineageDAG(memory, relation=DERIVED_FROM_RELATION),
            namespaces=anchors["namespaces"],
            trajectories=anchors["trajectories"],
        )

    @property
    def profile_root(self) -> MemoryBlock:
        """Return the block that owns profile identities and revisions."""

        return self.profiles.collection

    @property
    def prompt_root(self) -> MemoryBlock:
        """Return the block that owns prompt identities and revisions."""

        return self.prompts.collection

    @property
    def queue_root(self) -> MemoryBlock:
        """Return the block that owns queued submissions."""

        return self.queue.log


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, creating it when absent.

    Anchors are matched on metadata rather than on value so that a tier which
    keeps state in its own value can still be found again.
    """

    existing = next(
        (
            child
            for child in tiers.children()
            if child.type == ANCHOR_TYPE and child.metadata.get("tier") == name
        ),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(
        type=ANCHOR_TYPE,
        value={"tier": name},
        metadata={"tier": name},
    )
