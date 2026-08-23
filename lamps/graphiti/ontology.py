"""Prescribed and learned ontologies for the Graphiti-inspired lamp.

Graphiti lets developers either declare entity and edge types up front with
Pydantic models and an ``edge_type_map`` (a prescribed ontology) or let the
structure emerge from the ingested data (a learned ontology). Both choices are
application concerns, so this module owns them: Genie stores block types,
values, and metadata without attaching meaning to any of them.

The one piece of semantics this lamp needs beyond naming is cardinality.
Upstream Graphiti asks a language model whether a newly extracted fact
contradicts an existing one. A deterministic example cannot call a model, so an
edge type declares ``single_valued=True`` when a subject may hold only one
active object for that relation. Contradiction then becomes a rule the example
can check instead of a judgement it has to infer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

Declaration = TypeVar("Declaration", "EntityType", "EdgeType")


@dataclass(frozen=True, slots=True)
class EntityType:
    """One declared kind of entity node.

    Attributes:
        name: Type name stored on entity blocks, such as ``"person"``.
        description: Plain-language guidance for whoever extracts entities.
    """

    name: str
    description: str

    def __post_init__(self) -> None:
        """Validate that both fields carry usable text."""

        _require_text(self.name, field="EntityType name")
        _require_text(self.description, field="EntityType description")


@dataclass(frozen=True, slots=True)
class EdgeType:
    """One declared kind of relationship between entity nodes.

    ``subject_types`` and ``object_types`` are this lamp's compact stand-in for
    Graphiti's ``edge_type_map``, which lists the edge names allowed between
    each pair of entity types.

    Attributes:
        name: Relation name stored on fact blocks, such as ``"lives_in"``.
        description: Plain-language guidance for whoever extracts facts.
        subject_types: Entity types allowed at the source of the relation.
        object_types: Entity types allowed at the target of the relation.
        single_valued: Whether one subject may hold only one active object for
            this relation, so a new fact invalidates the previous one.
    """

    name: str
    description: str
    subject_types: frozenset[str]
    object_types: frozenset[str]
    single_valued: bool = False

    def __post_init__(self) -> None:
        """Validate the relation name, endpoint types, and cardinality flag."""

        _require_text(self.name, field="EdgeType name")
        _require_text(self.description, field="EdgeType description")
        if not isinstance(self.subject_types, frozenset) or not self.subject_types:
            raise ValueError(f"EdgeType {self.name!r} needs at least one subject type.")
        if not isinstance(self.object_types, frozenset) or not self.object_types:
            raise ValueError(f"EdgeType {self.name!r} needs at least one object type.")
        if not isinstance(self.single_valued, bool):
            raise TypeError(f"EdgeType {self.name!r} single_valued must be a boolean.")

    def allows(self, *, subject_type: str, object_type: str) -> bool:
        """Report whether this relation may connect two entity types.

        Args:
            subject_type: Entity type at the source of the proposed fact.
            object_type: Entity type at the target of the proposed fact.

        Returns:
            ``True`` when both endpoint types are declared for this relation.
        """

        return subject_type in self.subject_types and object_type in self.object_types


@dataclass(frozen=True, slots=True)
class Ontology:
    """Entity and edge types a graph accepts, or an open schema.

    A prescribed ontology validates every entity and fact before it is stored.
    A learned ontology accepts any name and treats every relation as
    multi-valued, which mirrors Graphiti's emergent-structure mode: without a
    declared cardinality there is no deterministic contradiction rule left, so
    nothing is invalidated automatically.

    Attributes:
        entity_types: Declared entity types by name, empty when learned.
        edge_types: Declared edge types by name, empty when learned.
        prescribed: Whether declarations are enforced.
    """

    entity_types: Mapping[str, EntityType]
    edge_types: Mapping[str, EdgeType]
    prescribed: bool = True

    @classmethod
    def prescribe(
        cls,
        *,
        entities: Iterable[EntityType],
        edges: Iterable[EdgeType],
    ) -> Ontology:
        """Declare a closed set of entity and edge types.

        Args:
            entities: Entity types the graph accepts.
            edges: Edge types the graph accepts.

        Returns:
            An ontology that rejects undeclared names and mismatched endpoints.

        Raises:
            ValueError: If a name repeats or an edge references an entity type
                that was not declared.
        """

        entity_types = _by_name(entities, label="entity type")
        edge_types = _by_name(edges, label="edge type")
        for edge in edge_types.values():
            unknown = (edge.subject_types | edge.object_types) - entity_types.keys()
            if unknown:
                raise ValueError(
                    f"Edge type {edge.name!r} references undeclared entity types "
                    f"{sorted(unknown)}."
                )
        return cls(entity_types=entity_types, edge_types=edge_types)

    @classmethod
    def learned(cls) -> Ontology:
        """Accept any entity or relation name without declaring it first.

        Returns:
            An open ontology with no cardinality rules.
        """

        return cls(
            entity_types=MappingProxyType({}),
            edge_types=MappingProxyType({}),
            prescribed=False,
        )

    def require_entity_type(self, entity_type: str) -> None:
        """Validate one entity type name.

        Args:
            entity_type: Type name proposed for an entity node.

        Raises:
            ValueError: If the name is empty, or undeclared in a prescribed
                ontology.
        """

        _require_text(entity_type, field="entity type")
        if self.prescribed and entity_type not in self.entity_types:
            raise ValueError(
                f"Entity type {entity_type!r} is not in the prescribed ontology "
                f"{sorted(self.entity_types)}."
            )

    def require_edge(
        self,
        relation: str,
        *,
        subject_type: str,
        object_type: str,
    ) -> EdgeType | None:
        """Validate one relation against its endpoint entity types.

        Args:
            relation: Relation name proposed for a fact.
            subject_type: Entity type at the source of the fact.
            object_type: Entity type at the target of the fact.

        Returns:
            The declared edge type, or ``None`` under a learned ontology.

        Raises:
            ValueError: If the relation is empty, undeclared, or connects
                endpoint types it was not declared for.
        """

        _require_text(relation, field="relation")
        if not self.prescribed:
            return None
        edge = self.edge_types.get(relation)
        if edge is None:
            raise ValueError(
                f"Relation {relation!r} is not in the prescribed ontology "
                f"{sorted(self.edge_types)}."
            )
        if not edge.allows(subject_type=subject_type, object_type=object_type):
            raise ValueError(
                f"Relation {relation!r} does not connect {subject_type!r} to "
                f"{object_type!r}."
            )
        return edge

    def is_single_valued(self, relation: str) -> bool:
        """Report whether a new fact should invalidate the previous one.

        Args:
            relation: Relation name of the incoming fact.

        Returns:
            ``True`` only when a prescribed edge type declares the relation
            single-valued.
        """

        edge = self.edge_types.get(relation)
        return edge is not None and edge.single_valued


def _by_name(values: Iterable[Declaration], *, label: str) -> Mapping[str, Declaration]:
    """Index declarations by name and reject duplicates."""

    indexed: dict[str, Declaration] = {}
    for value in values:
        if value.name in indexed:
            raise ValueError(f"Duplicate {label} {value.name!r}.")
        indexed[value.name] = value
    return MappingProxyType(indexed)


def _require_text(value: object, *, field: str) -> None:
    """Validate that a declaration field holds non-empty text."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value.strip():
        raise ValueError(f"{field} cannot be empty.")
