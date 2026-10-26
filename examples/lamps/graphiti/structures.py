"""Genie structure facades for the ownership tiers of the Graphiti lamp.

- **Upstream:** Graphiti divides its graph into 3 subgraphs: episodic,
  semantic-entity, and community.
- **Here:** these subgraphs are ownership tiers below one root block. Each tier
  is the anchor of the Genie facades that the lamp needs.
- **Why a separate module:** the module shows the composition in one place. It
  shows the facade for each Graphiti idea, and the backend capability that each
  facade needs.
- **Bi-temporality:** fact valid time and fact transaction time are 2 separate
  `TemporalCollection` facades over the same fact blocks. So each axis is
  independent of the other axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from examples.lamps.graphiti.model import ANCHOR_TYPE, SUPERSEDED_BY_RELATION
from genie import MemoryBlock, MemoryHandle
from genie.backend import AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore
from genie.structures import (
    EventLog,
    LineageDAG,
    OwnershipTree,
    PropertyGraph,
    TemporalCollection,
    VersionedRecords,
)


class GraphitiStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities that the Graphiti structure bundle needs."""


@dataclass(frozen=True, slots=True)
class GraphitiStructures:
    """The structural views that this lamp composes over one graph root.

    Attributes:
        tiers: Ownership view of the episodic, semantic, and community tiers.
        graph: Typed relationship view over episodes, facts, and entities.
        episodes: Append-only ingestion order of episodes.
        episode_time: Event time of each episode.
        valid_time: Fact valid time, `[valid_at, invalid_at)`.
        transaction_time: Fact transaction time, `[ingested_at, expired_at)`.
        entities: Entity identities keyed by type and name. Each identity is the
            anchor of an ordered log of summary revisions.
        succession: Supersession history between facts, with protection
            against cycles.
        community_root: Block that owns community nodes.
    """

    tiers: OwnershipTree
    graph: PropertyGraph
    episodes: EventLog
    episode_time: TemporalCollection
    valid_time: TemporalCollection
    transaction_time: TemporalCollection
    entities: VersionedRecords
    succession: LineageDAG
    community_root: MemoryBlock

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: GraphitiStructureStore,
    ) -> GraphitiStructures:
        """Build or reopen every structural anchor below one bounded root.

        - **Reuse:** the method scans the children of each tier to find the
          existing anchors. So a second call returns the same structures and
          does not make duplicates.
        - **Why a scan:** the anchors have generated ids, because a handle
          without `allow_custom_ids` refuses a caller-provided id. So the
          method finds each anchor by its type and its tier value.
        - **Succession protection:** the method protects
          `SUPERSEDED_BY_RELATION` through `store`, because a handle cannot.
          A repeated call changes nothing.

        Args:
            memory: Handle rooted at the block that owns the whole graph.
            store: Backend with the acyclic-link, keyed, sequence, and temporal
                capabilities.

        Returns:
            The structure bundle for that graph.

        Raises:
            MemoryConflict: If the stored links of `SUPERSEDED_BY_RELATION`
                contain a cycle.
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("GraphitiStructures requires a bounded MemoryHandle.")
        store.protect_relation(SUPERSEDED_BY_RELATION)
        tiers = OwnershipTree(memory, root=memory.root_id)

        def anchor(name: str, *, parent: MemoryBlock | None = None) -> MemoryBlock:
            container = tiers.root if parent is None else parent
            existing = next(
                (
                    child
                    for child in tiers.children(container)
                    if child.type == ANCHOR_TYPE and child.value == {"tier": name}
                ),
                None,
            )
            if existing is not None:
                return existing
            return tiers.create_child(
                type=ANCHOR_TYPE,
                value={"tier": name},
                metadata={"tier": name},
                parent=container,
            )

        episodic = anchor("episodic")
        semantic = anchor("semantic")
        return cls(
            tiers=tiers,
            graph=PropertyGraph(memory),
            episodes=EventLog(memory, store, log=episodic),
            episode_time=TemporalCollection(memory, store, collection=episodic),
            valid_time=TemporalCollection(
                memory,
                store,
                collection=anchor("valid_time", parent=semantic),
            ),
            transaction_time=TemporalCollection(
                memory,
                store,
                collection=anchor("transaction_time", parent=semantic),
            ),
            entities=VersionedRecords(memory, store, collection=semantic),
            succession=LineageDAG(memory, relation=SUPERSEDED_BY_RELATION),
            community_root=anchor("community"),
        )

    @property
    def episodic_root(self) -> MemoryBlock:
        """Return the block that owns episode nodes."""

        return self.episodes.log

    @property
    def semantic_root(self) -> MemoryBlock:
        """Return the block that owns entity and fact nodes."""

        return self.entities.collection


