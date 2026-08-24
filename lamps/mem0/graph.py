"""Mem0's graph memory: the entities in a conversation and how they relate.

With a graph store configured, Mem0 runs a second extraction over the same
messages. It pulls out entities, resolves each one against the nodes already
stored for that user so two spellings do not become two nodes, and writes typed
relationships between them. A search then returns both halves: the memories
that match the query text, and the relationships attached to the entities the
query names. The graph phase can also retract a relationship that a later turn
contradicts.

`PropertyGraph` carries the relationships and a second `KeyedCollection`
carries entity resolution: an entity is addressed by its folded name inside the
session's outermost identifier, which is the level upstream scopes graph nodes
at. So resolving an extracted name is one keyed lookup rather than a scan, and
listing a user's entities is one namespaced read.

Genie has no operation that removes a single link, so a retracted relationship
is marked rather than deleted. That fits the rest of the lamp, where a change is
something you can still read afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from genie import MemoryBlock, MemoryHandle, MemoryLink
from genie.structures import MemoryAddress
from lamps.mem0.model import (
    ENTITY_TYPE,
    READ_LIMIT,
    RELATION_PREFIX,
    GraphChange,
    GraphRelation,
    Session,
    Triplet,
    entity_key,
    format_timestamp,
    normalize_timestamp,
    parse_timestamp,
    relation_label,
    relation_name,
    required_text,
    tokenize,
)
from lamps.mem0.structures import Mem0Structures


class GraphMemory:
    """Entities and typed relationships, scoped like the memories beside them."""

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create graph memory over one bounded handle.

        Args:
            memory: Bounded Genie handle owning the entity tier.
            structures: Facade bundle built over the same handle.
        """

        self.memory = memory
        self.structures = structures

    def apply(self, session: Session, triplet: Triplet, *, at: datetime) -> GraphChange:
        """Add or retract one relationship, and report which happened.

        Args:
            session: Scope whose entity namespace the relationship belongs to.
            triplet: Candidate relationship from the graph extraction phase.
            at: Time recorded on the edge.

        Returns:
            The change that was made. A relationship already on file is left
            alone, and a retraction with nothing to retract writes nothing.
        """

        applied_at = normalize_timestamp(at, field="at")
        relation = relation_name(triplet.relation)
        existing = self._edge(session, triplet, relation)
        if triplet.retracts:
            if existing is None:
                return GraphChange(triplet=triplet, event="NONE", relation=None, reason="no matching relationship is on file")
            edge = self.structures.graph.update_edge(existing, metadata={"retracted_at": format_timestamp(applied_at)})
            return GraphChange(
                triplet=triplet,
                event="DELETE",
                relation=self._relation(session, edge),
                reason="a later turn contradicted the relationship",
            )
        if existing is not None:
            return GraphChange(
                triplet=triplet,
                event="NONE",
                relation=self._relation(session, existing),
                reason="the relationship is already on file",
            )
        edge = self.structures.graph.add_edge(
            self.ensure_entity(session, triplet.source),
            self.ensure_entity(session, triplet.target),
            relation,
            metadata={"created_at": format_timestamp(applied_at), "retracted_at": None, "scope": session.label},
        )
        return GraphChange(triplet=triplet, event="ADD", relation=self._relation(session, edge), reason="a new relationship was extracted")

    def relations(self, session: Session, *, limit: int = READ_LIMIT, include_retracted: bool = False) -> list[GraphRelation]:
        """Return every relationship stored in one scope's entity namespace.

        Args:
            session: Scope to read.
            limit: Maximum entities to walk.
            include_retracted: Whether relationships marked as no longer true
                are returned as well.

        Returns:
            Relationships in ascending source, relation, and target order. Each
            edge appears once, walked from the entity it starts at.
        """

        found: list[GraphRelation] = []
        for block in self.entities(session, limit=limit):
            for edge in self.structures.graph.edges(block, direction="out"):
                if not edge.relation.startswith(RELATION_PREFIX):
                    continue
                relation = self._relation(session, edge)
                if relation.retracted and not include_retracted:
                    continue
                found.append(relation)
        return _ordered(found)

    def relations_for(self, session: Session, query: str, *, limit: int = READ_LIMIT) -> list[GraphRelation]:
        """Return the relationships attached to the entities a query names.

        Upstream a model reads the query and names the entities in it. Here an
        entity is named when every token of its name appears in the query, which
        needs no model and is enough to show the two halves of a Mem0 search
        arriving together.

        Args:
            session: Scope to read.
            query: Search text.
            limit: Maximum entities to walk.

        Returns:
            Relationships in ascending source, relation, and target order,
            excluding retracted ones.
        """

        terms = set(tokenize(required_text(query, field="query")))
        found: list[GraphRelation] = []
        for block in self.entities(session, limit=limit):
            if not set(tokenize(_name(block))) <= terms:
                continue
            for neighbor in self.structures.graph.neighbors(block, direction="both"):
                if not neighbor.link.relation.startswith(RELATION_PREFIX):
                    continue
                relation = self._relation(session, neighbor.link)
                if not relation.retracted and relation not in found:
                    found.append(relation)
        return _ordered(found)

    def entities(self, session: Session, *, limit: int = READ_LIMIT) -> list[MemoryBlock]:
        """Return the entity nodes stored in one scope, in address order.

        Args:
            session: Scope whose entity namespace should be listed.
            limit: Maximum entities to return.

        Returns:
            Entity blocks in ascending key order.
        """

        items = self.structures.entities.items(namespace=self.namespace(session), limit=limit)
        return [item.block for item in items]

    def entity_names(self, session: Session, *, limit: int = READ_LIMIT) -> list[str]:
        """Return the display names of the entities stored in one scope.

        Args:
            session: Scope whose entity namespace should be listed.
            limit: Maximum entities to return.

        Returns:
            Entity names in ascending key order.
        """

        return [_name(block) for block in self.entities(session, limit=limit)]

    def entity(self, session: Session, name: str) -> MemoryBlock | None:
        """Resolve one extracted entity name against the stored nodes.

        Args:
            session: Scope whose entity namespace to look in.
            name: Entity name as extracted.

        Returns:
            The entity node, or `None` when the scope has no node for it.
        """

        return self.structures.entities.get(MemoryAddress(self.namespace(session), entity_key(name)))

    def ensure_entity(self, session: Session, name: str) -> MemoryBlock:
        """Return one entity node, creating it when the scope has none.

        Args:
            session: Scope whose entity namespace to place the node in.
            name: Entity name as extracted. The first spelling seen is the one
                reported afterwards.

        Returns:
            The entity node.
        """

        existing = self.entity(session, name)
        if existing is not None:
            return existing
        display = required_text(name, field="entity name")
        key = entity_key(display)
        namespace = self.namespace(session)
        block = self.structures.tiers.create_child(
            parent=self.structures.entity_root,
            type=ENTITY_TYPE,
            value={"name": display, "key": key, "scope": list(namespace)},
            metadata={"entity": key, "scope": "/".join(namespace)},
        )
        try:
            self.structures.entities.bind(MemoryAddress(namespace, key), block)
        except Exception:
            self.memory.delete(block)
            raise
        return block

    def drop(self, session: Session) -> list[GraphRelation]:
        """Delete one scope's entity namespace and every edge attached to it.

        Mem0 clears graph memory alongside the vector store when a scope is
        retired. Deleting the nodes is enough here: Genie removes the links of a
        deleted block, so the relationships go with the entities they connected
        rather than being unlinked one at a time.

        Args:
            session: Scope being retired. Nothing is deleted unless it names the
                whole entity namespace, so retiring one run leaves the user's
                entities alone.

        Returns:
            The relationships that were removed, retracted ones included.
        """

        if session.path != self.namespace(session):
            return []
        removed = self.relations(session, include_retracted=True)
        for block in self.entities(session):
            self.memory.delete(block)
        return removed

    def namespace(self, session: Session) -> tuple[str, ...]:
        """Return the entity namespace one session resolves names in.

        Mem0 scopes graph nodes by `user_id`, so the outermost identifier a
        session names is the level entities are shared at. Two runs of the same
        user therefore see one node per entity rather than one each.

        Args:
            session: Scope being read or written.

        Returns:
            The single-segment namespace entities are addressed inside.
        """

        return session.path[:1]

    def _edge(self, session: Session, triplet: Triplet, relation: str) -> MemoryLink | None:
        """Return the live edge matching one candidate, when there is one."""

        source = self.entity(session, triplet.source)
        target = self.entity(session, triplet.target)
        if source is None or target is None:
            return None
        for edge in self.structures.graph.edges(source, relation=relation, direction="out"):
            if edge.target_id == target.id and edge.metadata.get("retracted_at") is None:
                return edge
        return None

    def _relation(self, session: Session, edge: MemoryLink) -> GraphRelation:
        """Decode one edge into the relationship it represents."""

        retracted = edge.metadata.get("retracted_at")
        return GraphRelation(
            source=_name(self._node(session, edge.source_id)),
            relation=relation_label(edge.relation),
            target=_name(self._node(session, edge.target_id)),
            link_id=edge.id,
            retracted_at=None if retracted is None else parse_timestamp(str(retracted), field="retracted_at"),
        )

    def _node(self, session: Session, block_id: str) -> MemoryBlock:
        """Return one endpoint node, failing when it has gone."""

        block = self.memory.get(block_id)
        if block is None or block.type != ENTITY_TYPE:
            raise KeyError(f"Entity {block_id!r} is not stored in scope {session.label!r}.")
        return block


def _name(block: MemoryBlock) -> str:
    """Return the display name stored on one entity node.

    Args:
        block: Entity node.

    Returns:
        The name as first extracted.

    Raises:
        TypeError: If the block is not an entity node.
    """

    if block.type != ENTITY_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a Mem0 entity.")
    return required_text(cast(dict[str, Any], block.value).get("name"), field="entity name")


def _ordered(relations: list[GraphRelation]) -> list[GraphRelation]:
    """Return relationships in a stable, run-independent order.

    Args:
        relations: Relationships to sort.

    Returns:
        The same relationships ordered by source, relation, and target, because
        link ids are fresh on every run.
    """

    return sorted(relations, key=lambda relation: (relation.source, relation.relation, relation.target))
