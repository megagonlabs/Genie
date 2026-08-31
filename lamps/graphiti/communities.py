"""Community nodes for the Graphiti-inspired lamp.

- Upstream: Graphiti's third subgraph groups related entities into community
  nodes whose summaries collate the summaries of their members, giving a view
  above the granular facts stored on edges. It finds those groups with label
  propagation, so a group can be extended one entity at a time.
- Here: the lamp keeps the community subgraph but not the clustering algorithm.
  A community is a connected component of the entities linked by the facts valid
  on one bi-temporal slice. That is coarser than label propagation and needs no
  scoring, because the grouping falls out of the graph Genie already stores.
- What it demonstrates: community nodes are ordinary owned blocks with typed
  `has_member` edges, so a rebuild is a subtree delete and membership is a graph
  read.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime

from genie import MemoryBlock
from lamps.graphiti.model import COMMUNITY_TYPE, MEMBER_RELATION, Entity, required_string
from lamps.graphiti.temporal_graph import TemporalKnowledgeGraph

_SUMMARY_LIMIT = 6


@dataclass(frozen=True, slots=True)
class Community:
    """One group of entities connected on a time slice.

    Attributes:
        block: Stored community block holding the name and summary.
        members: Member entities ordered by name.
    """

    block: MemoryBlock
    members: tuple[Entity, ...]

    @property
    def id(self) -> str:
        """Return the community block id."""

        return self.block.id

    @property
    def name(self) -> str:
        """Return the community's display name."""

        return required_string(self.block.value, "name")

    @property
    def summary(self) -> str:
        """Return the collated summary of the community's members."""

        return required_string(self.block.value, "summary")

    @property
    def member_names(self) -> tuple[str, ...]:
        """Return member entity names in order."""

        return tuple(member.name for member in self.members)


class CommunityBuilder:
    """Derive and store the community subgraph for one time slice.

    Communities come from the facts valid on the slice, so the same graph groups
    differently at different points in time. That is the useful part of deriving
    them instead of storing them: the community subgraph is a projection of the
    semantic subgraph.
    """

    def __init__(self, graph: TemporalKnowledgeGraph) -> None:
        """Create a builder over one graph.

        Args:
            graph: Graph whose entities and facts are grouped.
        """

        self._graph = graph

    def build(self, *, as_of: datetime, known_at: datetime | None = None) -> list[Community]:
        """Rebuild the community subgraph from one bi-temporal slice.

        Existing community nodes are deleted first, matching Graphiti's own
        rebuild semantics. Entities the slice does not connect are left out
        rather than becoming a community of one.

        Args:
            as_of: Effective time whose valid facts define the connections.
            known_at: Optional ingestion time the facts must be on record at.

        Returns:
            The stored communities, ordered by name.
        """

        self.clear()
        adjacency = self._adjacency(as_of=as_of, known_at=known_at)
        connected = {
            entity.id: entity for entity in self._graph.entities() if entity.id in adjacency
        }
        communities = [
            self._store(members, adjacency=adjacency)
            for members in _components(connected, adjacency)
        ]
        return sorted(communities, key=lambda community: community.name)

    def communities(self) -> list[Community]:
        """Return the stored communities.

        Returns:
            Communities ordered by name.
        """

        blocks = [
            child
            for child in self._graph.memory.children(self._graph.structures.community_root)
            if child.type == COMMUNITY_TYPE
        ]
        return sorted((self._resolve(block) for block in blocks), key=lambda item: item.name)

    def community_of(self, entity: str | MemoryBlock | Entity) -> Community | None:
        """Return the community one entity belongs to, if any.

        Args:
            entity: Entity, block, or block id.

        Returns:
            The owning community, or `None` when the entity is unassigned.
        """

        block = self._graph.entity_of(entity).block
        for neighbor in self._graph.structures.graph.neighbors(
            block,
            relation=MEMBER_RELATION,
            direction="in",
        ):
            if neighbor.block.type == COMMUNITY_TYPE:
                return self._resolve(neighbor.block)
        return None

    def clear(self) -> None:
        """Delete every stored community node and its membership edges."""

        for community in self.communities():
            self._graph.memory.delete(community.block)

    def _adjacency(self, *, as_of: datetime, known_at: datetime | None) -> dict[str, Counter[str]]:
        """Build undirected entity adjacency from one bi-temporal slice."""

        adjacency: dict[str, Counter[str]] = {}
        for fact in self._graph.facts_at(as_of, known_at=known_at):
            adjacency.setdefault(fact.subject.id, Counter())[fact.object.id] += 1
            adjacency.setdefault(fact.object.id, Counter())[fact.subject.id] += 1
        return adjacency

    def _store(
        self,
        members: list[Entity],
        *,
        adjacency: dict[str, Counter[str]],
    ) -> Community:
        """Write one community node and link it to its members."""

        hub = min(
            members,
            key=lambda member: (-sum(adjacency.get(member.id, Counter()).values()), member.name),
        )
        name = f"{hub.name} community"
        block = self._graph.memory.put(
            parent=self._graph.structures.community_root,
            type=COMMUNITY_TYPE,
            value={"name": name, "summary": _summarize(members)},
            metadata={"name": name, "size": len(members)},
        )
        try:
            for member in members:
                self._graph.structures.graph.add_edge(block, member.block, MEMBER_RELATION)
        except Exception:
            self._graph.memory.delete(block)
            raise
        return Community(block=block, members=tuple(members))

    def _resolve(self, block: MemoryBlock) -> Community:
        """Resolve a community block into its ordered member entities."""

        members = [
            self._graph.entity_of(neighbor.block)
            for neighbor in self._graph.structures.graph.neighbors(block, relation=MEMBER_RELATION)
        ]
        return Community(
            block=block,
            members=tuple(sorted(members, key=lambda member: member.name)),
        )


def _components(
    entities: dict[str, Entity],
    adjacency: dict[str, Counter[str]],
) -> list[list[Entity]]:
    """Split connected entities into components, each ordered by name.

    Args:
        entities: Entities the slice connects, by id.
        adjacency: Undirected neighbor counts per entity.

    Returns:
        Components in ascending order of their first member's name.
    """

    seen: set[str] = set()
    components: list[list[Entity]] = []
    for start in sorted(entities, key=lambda entity_id: entities[entity_id].name):
        if start in seen:
            continue
        member_ids = [start]
        seen.add(start)
        pending = deque([start])
        while pending:
            for neighbor_id in sorted(adjacency.get(pending.popleft(), Counter())):
                if neighbor_id in entities and neighbor_id not in seen:
                    seen.add(neighbor_id)
                    member_ids.append(neighbor_id)
                    pending.append(neighbor_id)
        components.append(sorted((entities[member_id] for member_id in member_ids), key=lambda member: member.name))
    return components


def _summarize(members: list[Entity]) -> str:
    """Join member summaries into one community summary."""

    kept = [member.summary for member in members if member.summary]
    if not kept:
        return "No member summaries recorded."
    shown = kept[:_SUMMARY_LIMIT]
    text = " ".join(shown)
    if len(kept) > len(shown):
        text = f"{text} (+{len(kept) - len(shown)} more members)"
    return text
