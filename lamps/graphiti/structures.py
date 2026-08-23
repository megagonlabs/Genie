"""Genie structure facades wired to the Graphiti lamp's ownership tiers.

Graphiti describes its graph as three subgraphs: episodic, semantic-entity, and
community. Here those are ownership tiers below one root block, and each tier
anchors the Genie facades the lamp needs. Keeping the wiring in its own module
makes the composition itself readable: which facade each Graphiti idea maps to,
and which backend capability it requires.

Bi-temporality is the notable part. Fact valid time and fact transaction time
are two separate `TemporalCollection` facades over the same fact blocks, so
neither axis has to know the other exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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
from lamps.graphiti.model import ANCHOR_TYPE, SUPERSEDED_BY_RELATION


class GraphitiStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities required by the Graphiti structure bundle."""


@dataclass(frozen=True, slots=True)
class GraphitiStructures:
    """The structural views this lamp composes over one graph root.

    Attributes:
        tiers: Ownership view of the episodic, semantic, and community tiers.
        graph: Typed relationship view over episodes, facts, and entities.
        episodes: Append-only ingestion order of episodes.
        episode_time: Event time of each episode.
        valid_time: Fact valid time, `[valid_at, invalid_at)`.
        transaction_time: Fact transaction time, `[ingested_at, expired_at)`.
        entities: Entity identities keyed by type and name, each anchoring an
            ordered log of summary revisions.
        succession: Cycle-protected supersession history between facts.
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

        Existing anchors are found by scanning the tier they live in, so
        reopening a graph returns the same structures instead of duplicating
        them. A bounded handle refuses ids outside its root, so the scan is
        used rather than a direct lookup of the derived anchor id.

        Args:
            memory: Handle rooted at the block that owns the whole graph.
            store: Backend providing acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that graph.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("GraphitiStructures requires a bounded MemoryHandle.")
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
                id=f"{tiers.root_id}:graphiti:{name}",
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


