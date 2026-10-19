"""The graph memory of Mem0: the entities in a conversation and their relationships.

If the application configures a graph store, Mem0 runs a second extraction over
the same messages:

- **Entities:** it extracts entities and resolves each one against the stored
  nodes of that user. Thus 2 spellings do not become 2 nodes.
- **Relationships:** it writes typed relationships between the entities.
- **Search:** a search then returns 2 halves: the memories that match the query
  text, and the relationships of the entities that the query names.
- **Retraction:** the graph phase can also retract a relationship that a later
  turn contradicts.

The Genie side:

- **`PropertyGraph`:** holds the relationships.
- **A second `KeyedCollection`:** does entity resolution. The address of an
  entity is its case-folded name in the outermost identifier of the session.
  Upstream scopes graph nodes at this level. Thus the resolution of an extracted
  name is one keyed lookup, not a scan. A list of the entities of a user is one
  read of one namespace.
- **The namespace is a block:** the namespace block owns the nodes in it. Thus
  one `delete_subtree` clears the graph of a user. The same operation retires
  the memories of that user.
- **Retraction marks the edge:** Genie has no operation that removes a single
  link. Thus the lamp marks a retracted relationship and does not delete it.
  This agrees with the rest of the lamp, where a caller can still read a change
  after it occurs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from examples.lamps.mem0.model import (
    ENTITY_TYPE,
    READ_LIMIT,
    RELATION_PREFIX,
    SCOPE_TYPE,
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
from examples.lamps.mem0.structures import Mem0Structures
from genie import MemoryBlock, MemoryHandle, MemoryLink
from genie.structures import MemoryAddress


class GraphMemory:
    """Entities and typed relationships, with the same scopes as the memories."""

    def __init__(self, memory: MemoryHandle, *, structures: Mem0Structures) -> None:
        """Create graph memory over one bounded handle.

        Args:
            memory: Bounded Genie handle that owns the entity tier.
            structures: Facade bundle over the same handle.
        """

        self.memory = memory
        self.structures = structures

    def apply(self, session: Session, triplet: Triplet, *, at: datetime) -> GraphChange:
        """Add or retract one relationship, and report the result.

        Args:
            session: Scope of the entity namespace that contains the
                relationship.
            triplet: Candidate relationship from the graph extraction phase.
            at: Time that the method records on the edge.

        Returns:
            The change. If the graph has the relationship already, the method
            writes nothing and returns `NONE`. A retraction with no relationship
            to retract also writes nothing and returns `NONE`.
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
        """Return all relationships in the entity namespace of one scope.

        Args:
            session: Scope to read.
            limit: Maximum number of entities to read.
            include_retracted: If `True`, the result also includes the
                relationships that are marked as no longer true.

        Returns:
            Relationships in ascending order of source, relation, and target.
            Each edge occurs once, from the entity at its start.
        """

        return self._relations_of(self.entities(session, limit=limit), session, include_retracted=include_retracted)

    def relations_for(self, session: Session, query: str, *, limit: int = READ_LIMIT) -> list[GraphRelation]:
        """Return the relationships that start or end at the entities that a query names.

        - **Upstream:** a model reads the query and names the entities in it.
        - **Here:** a query names an entity if each token of the entity name
          occurs in the query. This needs no model, and it is sufficient to
          show the 2 halves of a Mem0 search together.

        Args:
            session: Scope to read.
            query: Search text.
            limit: Maximum number of entities to read.

        Returns:
            Relationships in ascending order of source, relation, and target.
            Retracted relationships are not in the result.
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
        """Return the entity nodes in one scope, in address order.

        Args:
            session: Scope of the entity namespace to list.
            limit: Maximum number of entities to return.

        Returns:
            Entity blocks in ascending key order.
        """

        items = self.structures.entities.items(namespace=self.namespace(session), limit=limit)
        return [item.block for item in items]

    def entity_names(self, session: Session, *, limit: int = READ_LIMIT) -> list[str]:
        """Return the display names of the entities in one scope.

        Args:
            session: Scope of the entity namespace to list.
            limit: Maximum number of entities to return.

        Returns:
            Entity names in ascending key order.
        """

        return [_name(block) for block in self.entities(session, limit=limit)]

    def entity(self, session: Session, name: str) -> MemoryBlock | None:
        """Resolve one extracted entity name against the stored nodes.

        Args:
            session: Scope of the entity namespace to search.
            name: Entity name as extracted.

        Returns:
            The entity node, or `None` if the scope has no node for it.
        """

        return self.structures.entities.get(MemoryAddress(self.namespace(session), entity_key(name)))

    def ensure_entity(self, session: Session, name: str) -> MemoryBlock:
        """Return one entity node, and create it if the scope has no node for it.

        Args:
            session: Scope of the entity namespace for the node.
            name: Entity name as extracted. The method keeps the first spelling
                and reports that spelling later.

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
            parent=self._ensure_scope(session),
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
        """Delete the entity namespace of one scope and all edges of its entities.

        - **Upstream:** Mem0 clears the graph memory together with the vector
          store when it retires a scope.
        - **Here:** each namespace owns its nodes. Thus one `delete_subtree`
          retires the namespace, and no walk deletes each node in turn.
        - **Why a subtree:** the memory tier uses the same reason. A list that
          deletes what it read can retire only as much as one read returns. A
          subtree is always the full namespace.
        - **Edges and bindings:** Genie removes the links and the keyed
          bindings of a block when it deletes the block. Thus the edges and the
          keyed bindings go away with the blocks.

        Args:
            session: Scope to retire. The method deletes nothing if the session
                path has more segments than the entity namespace. Thus a retire
                of one run does not change the entities of the user.

        Returns:
            The removed relationships, retracted relationships included.
        """

        if session.path != self.namespace(session):
            return []
        node = self._scope_node(session)
        if node is None:
            return []
        nodes = [block for block in self.structures.tiers.children(node) if block.type == ENTITY_TYPE]
        removed = self._relations_of(nodes, session, include_retracted=True)
        self.structures.tiers.delete_subtree(node)
        return removed

    def namespace(self, session: Session) -> tuple[str, ...]:
        """Return the entity namespace in which one session resolves names.

        Mem0 scopes graph nodes by `user_id`. Thus the outermost identifier of
        a session is the level where the session shares entities. 2 runs of the
        same user see one node for each entity, not one node for each run.

        Args:
            session: Scope to read or write.

        Returns:
            The namespace of one segment that contains the entity addresses.
        """

        return session.path[:1]

    def _scope_node(self, session: Session) -> MemoryBlock | None:
        """Return the block that owns one entity namespace, if it exists."""

        segment = self.namespace(session)[0]
        return next(
            (
                child
                for child in self.structures.tiers.children(self.structures.entity_root)
                if child.type == SCOPE_TYPE and child.metadata.get("segment") == segment
            ),
            None,
        )

    def _ensure_scope(self, session: Session) -> MemoryBlock:
        """Return the block that owns one entity namespace, and create it if it does not exist."""

        existing = self._scope_node(session)
        if existing is not None:
            return existing
        namespace = self.namespace(session)
        return self.structures.tiers.create_child(
            parent=self.structures.entity_root,
            type=SCOPE_TYPE,
            value={"segment": namespace[0], "path": list(namespace)},
            metadata={"segment": namespace[0], "scope": "/".join(namespace)},
        )

    def _relations_of(self, blocks: list[MemoryBlock], session: Session, *, include_retracted: bool) -> list[GraphRelation]:
        """Return the relationships that start at a set of entity nodes."""

        found: list[GraphRelation] = []
        for block in blocks:
            for edge in self.structures.graph.edges(block, direction="out"):
                if not edge.relation.startswith(RELATION_PREFIX):
                    continue
                relation = self._relation(session, edge)
                if relation.retracted and not include_retracted:
                    continue
                found.append(relation)
        return _ordered(found)

    def _edge(self, session: Session, triplet: Triplet, relation: str) -> MemoryLink | None:
        """Return the edge that matches one candidate and is not retracted, if one exists."""

        source = self.entity(session, triplet.source)
        target = self.entity(session, triplet.target)
        if source is None or target is None:
            return None
        for edge in self.structures.graph.edges(source, relation=relation, direction="out"):
            if edge.target_id == target.id and edge.metadata.get("retracted_at") is None:
                return edge
        return None

    def _relation(self, session: Session, edge: MemoryLink) -> GraphRelation:
        """Decode one edge into its relationship."""

        retracted = edge.metadata.get("retracted_at")
        return GraphRelation(
            source=_name(self._node(session, edge.source_id)),
            relation=relation_label(edge.relation),
            target=_name(self._node(session, edge.target_id)),
            link_id=edge.id,
            retracted_at=None if retracted is None else parse_timestamp(str(retracted), field="retracted_at"),
        )

    def _node(self, session: Session, block_id: str) -> MemoryBlock:
        """Return one endpoint node, and raise `KeyError` if it is not an entity in the store."""

        block = self.memory.get(block_id)
        if block is None or block.type != ENTITY_TYPE:
            raise KeyError(f"Entity {block_id!r} is not stored in scope {session.label!r}.")
        return block


def _name(block: MemoryBlock) -> str:
    """Return the display name on one entity node.

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
    """Return the relationships in a stable order that is the same in each run.

    Args:
        relations: Relationships to sort.

    Returns:
        The same relationships, ordered by source, relation, and target. The
        order does not use link ids, because link ids are new in each run.
    """

    return sorted(relations, key=lambda relation: (relation.source, relation.relation, relation.target))
