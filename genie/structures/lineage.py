"""Lineage DAG facade over relation-scoped property-graph edges."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from genie.memory.errors import MemoryNotFound
from genie.memory.links import LinkRelations, MemoryLink
from genie.structures.access import AcyclicLinkWriter, BlockRef, LinkDirection
from genie.structures.graph import GraphAccess, PropertyGraph
from genie.structures.records import GraphNeighbor, GraphPath


@runtime_checkable
class LineageAccess(GraphAccess, AcyclicLinkWriter, Protocol):
    """Access surface required by ``LineageDAG``."""


class LineageDAG:
    """A directed acyclic lineage view over existing typed links.

    One exact link relation identifies the DAG. Edges point from a predecessor
    to a successor. Reads and metadata updates use ``PropertyGraph``; insertion
    uses the access object's atomic acyclic-link operation.
    """

    __slots__ = ("_access", "_graph", "relation")

    def __init__(self, access: LineageAccess, *, relation: str) -> None:
        """Create a lineage DAG for one application-defined relation.

        Args:
            access: Full or scoped graph access with atomic DAG insertion.
            relation: Exact link relation that identifies this DAG. Use a
                distinct relation for each independent lineage graph.

        Raises:
            TypeError: If ``relation`` is not a string.
            ValueError: If ``relation`` is empty or reserved for mounts.
        """

        _validate_relation(relation)
        self._access = access
        self._graph = PropertyGraph(access)
        self.relation = relation

    def add(
        self,
        predecessor: BlockRef,
        successor: BlockRef,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Atomically add a predecessor-to-successor edge.

        Args:
            predecessor: Existing source block that came earlier.
            successor: Existing target block derived later.
            metadata: Optional edge properties such as provenance details.

        Returns:
            Created typed link.

        Raises:
            AccessDenied: If scope or policy rejects either endpoint.
            MemoryConflict: If the edge would create a directed cycle.
            MemoryNotFound: If either endpoint is missing.
            UnsupportedOperation: If the backend cannot insert atomically.
        """

        return self._access.add_acyclic_link(
            predecessor,
            successor,
            self.relation,
            metadata=metadata,
        )

    def update_edge(
        self,
        edge: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge metadata into one lineage edge without changing topology."""

        stored = self._require_stored_edge(edge)
        self._require_relation(stored)
        return self._graph.update_edge(stored, metadata=metadata)

    def edges(
        self,
        block: BlockRef,
        *,
        direction: LinkDirection = "out",
    ) -> list[MemoryLink]:
        """Return visible lineage edges connected to one block."""

        return self._graph.edges(
            block,
            relation=self.relation,
            direction=direction,
        )

    def predecessors(self, block: BlockRef) -> list[GraphNeighbor]:
        """Return visible direct predecessors connected by incoming edges."""

        return self._graph.neighbors(
            block,
            relation=self.relation,
            direction="in",
        )

    def successors(self, block: BlockRef) -> list[GraphNeighbor]:
        """Return visible direct successors connected by outgoing edges."""

        return self._graph.neighbors(
            block,
            relation=self.relation,
            direction="out",
        )

    def walk_predecessors(
        self,
        block: BlockRef,
        *,
        max_depth: int = 6,
        limit: int = 100,
    ) -> list[GraphPath]:
        """Return bounded lineage paths walking toward earlier blocks."""

        return self._graph.walk(
            block,
            relation=self.relation,
            direction="in",
            max_depth=max_depth,
            limit=limit,
        )

    def walk_successors(
        self,
        block: BlockRef,
        *,
        max_depth: int = 6,
        limit: int = 100,
    ) -> list[GraphPath]:
        """Return bounded lineage paths walking toward derived blocks."""

        return self._graph.walk(
            block,
            relation=self.relation,
            direction="out",
            max_depth=max_depth,
            limit=limit,
        )

    def shortest_path(
        self,
        predecessor: BlockRef,
        successor: BlockRef,
        *,
        max_depth: int = 6,
    ) -> GraphPath | None:
        """Return the shortest visible forward lineage path, if one exists."""

        return self._graph.shortest_path(
            predecessor,
            successor,
            relation=self.relation,
            direction="out",
            max_depth=max_depth,
        )

    def _require_relation(self, edge: MemoryLink) -> None:
        """Require an edge belonging to this relation-scoped DAG."""

        if edge.relation != self.relation:
            raise ValueError(
                f"Expected lineage relation {self.relation!r}, got "
                f"{edge.relation!r}."
            )

    def _require_stored_edge(self, edge: MemoryLink) -> MemoryLink:
        """Return the canonical visible edge instead of trusting caller fields."""

        stored = next(
            (
                candidate
                for candidate in self._graph.edges(edge.source_id, direction="out")
                if candidate.id == edge.id
            ),
            None,
        )
        if stored is None:
            raise MemoryNotFound(f"Memory link {edge.id!r} does not exist.")
        return stored


def _validate_relation(relation: object) -> None:
    """Require a non-empty, non-mount relation string."""

    if not isinstance(relation, str):
        raise TypeError("relation must be a string.")
    if not relation.strip():
        raise ValueError("relation must not be empty.")
    if relation == LinkRelations.MOUNT:
        raise ValueError("The reserved mount relation cannot identify a lineage DAG.")
