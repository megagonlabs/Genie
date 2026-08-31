"""Genie structure facades wired to the Hindsight lamp's ownership tiers.

A Hindsight memory bank is an isolated brain: one bank never reads another
bank's memories.

- Here a bank is a root block plus a bounded `MemoryHandle`.
- Its internal separation - documents, extracted facts, entities, observations,
  and mental models - becomes ownership tiers below that root.
- Each tier anchors the facades that tier needs, so deleting a bank deletes
  everything in it and deleting a tier deletes the layer it holds.

Two wiring choices carry most of the model:

- Facts join one `TemporalCollection` that holds both of Hindsight's time axes
  at once. The event time is when the bank learned the fact, and the validity
  interval is the period the fact describes.
- Observations and mental models are `VersionedRecords`, because both are
  refined rather than overwritten and both must be able to show what they used
  to say.
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
from lamps.hindsight.model import ANCHOR_TYPE, DERIVED_FROM_RELATION

TIERS = ("documents", "memories", "entities", "observations", "mental_models")


class HindsightStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities required by the Hindsight structure bundle."""


@dataclass(frozen=True, slots=True)
class HindsightStructures:
    """The structural views this lamp composes over one memory bank.

    Attributes:
        tiers: Ownership view of the five tiers below the bank root.
        graph: Entity mentions and the associative links between facts.
        lineage: Acyclic derivation chain from documents through facts and
            observations to mental-model revisions.
        retention: Append-only order in which facts were learned, and the
            cursor consolidation reads from.
        timeline: Learned-at event time and described-period validity of every
            fact.
        entities: Canonical entities addressed by every surface form that has
            resolved to them.
        observations: Consolidated beliefs, one revision per refinement.
        mental_models: Standing answers, one revision per rebuild.
        documents: Block that owns retained source payloads.
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
        """Build or reopen every structural anchor below one bounded root.

        Existing anchors are found by scanning the root's children, so
        reopening a bank returns the same structures instead of duplicating
        them. A bounded handle refuses ids outside its root, so the scan is
        used rather than a direct lookup of the derived anchor id.

        Args:
            memory: Handle rooted at the block that owns the whole bank.
            store: Backend providing acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that bank.

        Raises:
            ValueError: If the handle is unbounded.
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
    """Return the tier anchor with this name, creating it when absent.

    Anchors are matched on metadata rather than value, because the
    consolidation cursor lives in the observations anchor's value and would
    otherwise stop the tier from being found again.
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
