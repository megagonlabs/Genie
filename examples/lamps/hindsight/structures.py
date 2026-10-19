"""Genie structure facades connected to the ownership tiers of the Hindsight lamp.

A Hindsight memory bank is isolated. One bank never reads the memories of
another bank.

- **Bank:** here a bank is a root block plus a bounded `MemoryHandle`.
- **Tiers:** the internal layers of a bank (documents, extracted facts,
  entities, observations, and mental models) are ownership tiers below that
  root.
- **Delete scope:** each tier anchors the facades that the tier uses. So a
  delete of the bank deletes everything in it, and a delete of a tier deletes
  the layer in that tier.

2 connection choices hold most of the model:

- **One timeline for 2 time axes:** facts join one `TemporalCollection` that
  holds both Hindsight time axes. The event time is when the bank learned the
  fact. The validity interval is the period that the fact describes.
- **Versioned beliefs and answers:** observations and mental models are
  `VersionedRecords`. The lamp refines both and does not overwrite them, so both
  can show their earlier wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from examples.lamps.hindsight.model import ANCHOR_TYPE, DERIVED_FROM_RELATION
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

TIERS = ("documents", "memories", "entities", "observations", "mental_models")


class HindsightStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities that the Hindsight structure bundle uses."""


@dataclass(frozen=True, slots=True)
class HindsightStructures:
    """The structure views that this lamp composes over one memory bank.

    Attributes:
        tiers: Ownership view of the 5 tiers below the bank root.
        graph: Entity mentions and the associative links between facts.
        lineage: Acyclic derivation chain from documents through facts and
            observations to mental-model revisions.
        retention: Append-only order in which the bank learned facts.
            Consolidation reads it from a cursor.
        timeline: Learned-at event time and described-period validity of every
            fact.
        entities: Canonical entities. Every surface form that resolved to an
            entity is an address of that entity.
        observations: Consolidated beliefs, with 1 revision for each
            refinement.
        mental_models: Standing answers, with 1 revision for each rebuild.
        documents: Block that owns the retained source payloads.
    """

    tiers: OwnershipTree
    graph: PropertyGraph
    lineage: LineageDAG
    retention: EventLog
    timeline: TemporalCollection
    entities: KeyedCollection
    observations: VersionedRecords
    mental_models: VersionedRecords
    documents: MemoryBlock

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: HindsightStructureStore,
    ) -> HindsightStructures:
        """Build or reopen every structure anchor below one bounded root.

        - **Reopen:** the method finds the anchors that already exist in the
          children of the root. So a second call returns the same structures and does not make
          duplicates.
        - **Why a scan:** a bounded handle treats an unknown id as outside its
          root and raises `AccessDenied`. So a direct read of the derived
          anchor id fails before the anchor exists, and the method scans the
          children instead.

        Args:
            memory: Handle with its root at the block that owns the whole bank.
            store: Backend with the acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that bank.

        Raises:
            ValueError: If the handle is unbounded (it has no root).
        """

        if memory.root_id is None:
            raise ValueError("HindsightStructures requires a bounded MemoryHandle.")
        tiers = OwnershipTree(memory, root=memory.root_id)
        anchors = {name: _anchor(tiers, name) for name in TIERS}
        return cls(
            tiers=tiers,
            graph=PropertyGraph(memory),
            lineage=LineageDAG(memory, relation=DERIVED_FROM_RELATION),
            retention=EventLog(memory, store, log=anchors["memories"]),
            timeline=TemporalCollection(memory, store, collection=anchors["memories"]),
            entities=KeyedCollection(memory, store, collection=anchors["entities"]),
            observations=VersionedRecords(memory, store, collection=anchors["observations"]),
            mental_models=VersionedRecords(memory, store, collection=anchors["mental_models"]),
            documents=anchors["documents"],
        )

    @property
    def memories(self) -> MemoryBlock:
        """Return the block that owns extracted fact blocks."""

        return self.retention.log

    @property
    def entity_root(self) -> MemoryBlock:
        """Return the block that owns canonical entity blocks."""

        return self.entities.collection

    @property
    def observation_root(self) -> MemoryBlock:
        """Return the block that owns observation identities and revisions."""

        return self.observations.collection

    @property
    def model_root(self) -> MemoryBlock:
        """Return the block that owns mental-model identities and revisions."""

        return self.mental_models.collection


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, and create it if it does not exist.

    The function matches anchors on metadata, not on value. The consolidation
    cursor is in the value of the observations anchor. So a match on value
    would not find that tier again.
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
        id=f"{tiers.root_id}:hindsight:{name}",
        type=ANCHOR_TYPE,
        value={"tier": name},
        metadata={"tier": name},
    )
