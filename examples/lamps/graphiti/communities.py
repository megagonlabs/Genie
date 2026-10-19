"""Community nodes for the Graphiti lamp.

- **Upstream:** the third subgraph of Graphiti groups related entities into
  community nodes. The summary of a community node collects the summaries of its
  members. This gives a view above the detailed facts on the edges. Graphiti
  finds the groups with label propagation, so it can add 1 entity at a time to a
  group.
- **Here:** the lamp keeps the community subgraph but not the clustering
  algorithm. A community is a connected component of entities. The links come
  from the facts that are valid on one bi-temporal slice. This rule is less
  exact than label propagation. It needs no scoring, because the groups come
  directly from the graph that Genie already stores.
- **What it shows:** community nodes are ordinary owned blocks with typed
  `has_member` edges. So a rebuild is a subtree delete, and a membership check
  is a graph read.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime

from examples.lamps.graphiti.model import COMMUNITY_TYPE, MEMBER_RELATION, Entity, required_string
from examples.lamps.graphiti.temporal_graph import TemporalKnowledgeGraph
from genie import MemoryBlock

_SUMMARY_LIMIT = 6


@dataclass(frozen=True, slots=True)
class Community:
    """One group of connected entities on a time slice.

    Attributes:
        block: Stored community block that holds the name and the summary.
        members: Member entities, in name order.
    """

    block: MemoryBlock
    members: tuple[Entity, ...]

    @property
    def id(self) -> str:
        """Return the community block id."""

        return self.block.id

    @property
    def name(self) -> str:
        """Return the display name of the community."""

        return required_string(self.block.value, "name")

    @property
    def summary(self) -> str:
        """Return the joined summaries of the community members."""

        return required_string(self.block.value, "summary")

    @property
    def member_names(self) -> tuple[str, ...]:
        """Return the names of the member entities, in name order."""

        return tuple(member.name for member in self.members)


class CommunityBuilder:
    """Derive and store the community subgraph for one time slice.

    - **Source:** communities come from the facts that are valid on the slice.
      So the same graph gives different groups at different times.
    - **Why derived:** this time dependence is the useful part of a derived
      community subgraph. The community subgraph is a projection of the
      semantic subgraph.
    """

    def __init__(self, graph: TemporalKnowledgeGraph) -> None:
        """Create a builder over one graph.

        Args:
            graph: Graph with the entities and facts to group.
        """

        self._graph = graph

    def build(self, *, as_of: datetime, known_at: datetime | None = None) -> list[Community]:
        """Rebuild the community subgraph from one bi-temporal slice.

        - **Full rebuild:** the method first deletes the existing community
          nodes. Graphiti does the same in its rebuild.
        - **No community of 1:** the method leaves out entities that the slice
          does not connect.

        Args:
            as_of: Effective time. The facts that are valid at this time define
                the connections.
            known_at: Optional ingestion time. If you give it, only facts that
                were on record at this time define the connections.

        Returns:
            The stored communities, in name order.
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
            Communities in name order.
        """

        blocks = [
            child
            for child in self._graph.memory.children(self._graph.structures.community_root)
            if child.type == COMMUNITY_TYPE
        ]
        return sorted((self._resolve(block) for block in blocks), key=lambda item: item.name)

    def community_of(self, entity: str | MemoryBlock | Entity) -> Community | None:
        """Return the community of one entity, if it has one.

        Args:
            entity: Entity, block, or block id.

        Returns:
            The community that has the entity as a member, or `None` if the
            entity is in no community.
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
        """Build the undirected entity adjacency from one bi-temporal slice."""

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
        """Resolve a community block into its member entities, in name order."""

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
    """Split the connected entities into components, each in name order.

    Args:
        entities: Entities that the slice connects, by id.
        adjacency: Undirected neighbor counts for each entity.

    Returns:
        Components in ascending order of the name of their first member.
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
    """Join the member summaries into one community summary."""

    kept = [member.summary for member in members if member.summary]
    if not kept:
        return "No member summaries recorded."
    shown = kept[:_SUMMARY_LIMIT]
    text = " ".join(shown)
    if len(kept) > len(shown):
        text = f"{text} (+{len(kept) - len(shown)} more members)"
    return text
