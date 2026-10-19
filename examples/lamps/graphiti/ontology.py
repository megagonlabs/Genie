"""Prescribed and learned ontologies for the Graphiti lamp.

Graphiti has 2 ways to define structure:

- **Prescribed ontology:** the application declares entity and edge types
  before ingestion, with Pydantic models and an `edge_type_map`.
- **Learned ontology:** the structure comes from the ingested data.

Both ontologies are application concerns, so this module owns them. Genie stores
block types, values, and metadata, and gives them no meaning.

Cardinality:

- **Only extra meaning:** cardinality is the only meaning that this lamp needs
  in addition to names.
- **Upstream:** Graphiti asks a language model if a new extracted fact
  contradicts an existing fact.
- **Here:** a deterministic example cannot call a model. So an edge type
  declares `single_valued=True` when a subject can hold only 1 active object for
  that relation.
- **Result:** a contradiction is a rule that the example can check, not a
  judgement that it infers.
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
        name: Type name stored on entity blocks, such as `"person"`.
        description: Guidance in plain language for the extractor of entities.
    """

    name: str
    description: str

    def __post_init__(self) -> None:
        """Validate that both fields hold text that is not empty."""

        _require_text(self.name, field="EntityType name")
        _require_text(self.description, field="EntityType description")


@dataclass(frozen=True, slots=True)
class EdgeType:
    """One declared kind of relationship between entity nodes.

    `subject_types` and `object_types` are a short replacement in this lamp for
    the Graphiti `edge_type_map`. The `edge_type_map` lists the edge names that
    each pair of entity types can use.

    Attributes:
        name: Relation name stored on fact blocks, such as `"lives_in"`.
        description: Guidance in plain language for the extractor of facts.
        subject_types: Entity types that can be at the source of the relation.
        object_types: Entity types that can be at the target of the relation.
        single_valued: `True` if one subject can hold only 1 active object for
            this relation. Then a new fact invalidates the previous fact.
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
        """Report if this relation can connect 2 entity types.

        Args:
            subject_type: Entity type at the source of the proposed fact.
            object_type: Entity type at the target of the proposed fact.

        Returns:
            `True` if this relation declares both endpoint types.
        """

        return subject_type in self.subject_types and object_type in self.object_types


@dataclass(frozen=True, slots=True)
class Ontology:
    """Entity and edge types that a graph accepts, or an open schema.

    - **Prescribed:** the ontology validates every entity and fact before the
      graph stores it.
    - **Learned:** the ontology accepts any name and treats every relation as
      multi-valued. This is the same as the Graphiti mode where the structure
      comes from the data.
    - **No automatic invalidation when learned:** without a declared
      cardinality, no deterministic contradiction rule is left. So the graph
      invalidates nothing automatically.

    Attributes:
        entity_types: Declared entity types by name. Empty when learned.
        edge_types: Declared edge types by name. Empty when learned.
        prescribed: `True` if the ontology enforces the declarations.
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
            entities: Entity types that the graph accepts.
            edges: Edge types that the graph accepts.

        Returns:
            An ontology that rejects undeclared names and endpoint types that
            do not match.

        Raises:
            ValueError: If 2 declarations use the same name, or an edge refers
                to an entity type that the ontology does not declare.
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
        """Accept any entity or relation name without a declaration.

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
            entity_type: Proposed type name for an entity node.

        Raises:
            ValueError: If the name is empty, or if a prescribed ontology does
                not declare it.
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
            relation: Proposed relation name for a fact.
            subject_type: Entity type at the source of the fact.
            object_type: Entity type at the target of the fact.

        Returns:
            The declared edge type, or `None` for a learned ontology.

        Raises:
            ValueError: If the relation is empty or not declared, or if its
                declaration does not include these endpoint types.
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
        """Report if a new fact invalidates the previous fact for this relation.

        Args:
            relation: Relation name of the new fact.

        Returns:
            `True` only if a prescribed edge type declares the relation as
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
    """Validate that a declaration field holds text that is not empty."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value.strip():
        raise ValueError(f"{field} cannot be empty.")
