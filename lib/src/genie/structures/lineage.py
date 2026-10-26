"""Lineage DAG facade over the property-graph edges of one relation."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from genie.memory.errors import MemoryNotFound
from genie.memory.links import LinkRelations, MemoryLink
from genie.structures.access import AcyclicLinkWriter, BlockRef, LinkDirection
from genie.structures.graph import GraphAccess, PropertyGraph
from genie.structures.records import GraphNeighbor, GraphPath


@runtime_checkable
class LineageAccess(GraphAccess, AcyclicLinkWriter, Protocol):
    """Access surface that `LineageDAG` requires."""


class LineageDAG:
    """A directed acyclic lineage view over existing typed links.

    - **One relation:** one exact link relation identifies the DAG.
    - **Protection first:** trusted code calls `Memory.protect_relation` for
      the relation before the first `add`. The facade does not protect it.
    - **Direction:** edges point from a predecessor to a successor.
    - **Reads and updates:** reads and metadata updates use `PropertyGraph`.
    - **Inserts:** inserts use the atomic acyclic-link operation of the access
      object.
    """

    __slots__ = ("_access", "_graph", "relation")

    def __init__(self, access: LineageAccess, *, relation: str) -> None:
        """Create a lineage DAG for one application-defined relation.

        Args:
            access: Full or scoped graph access that can insert DAG edges
                atomically.
            relation: Exact link relation that identifies this DAG. The whole
                backend shares relation names. A distinct prefixed name for
                each lineage graph, such as `"billing.derived_from"`, keeps the
                graphs apart. `Memory.protect_relation` protects it before the
                first `add`.

        Raises:
            TypeError: If `relation` is not a string.
            ValueError: If `relation` is empty, contains only whitespace, or is
                the reserved mount relation.
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
        """Add a predecessor-to-successor edge in one atomic step.

        Args:
            predecessor: Existing source block that came earlier.
            successor: Existing target block that came later from it.
            metadata: Optional edge properties, such as provenance details.

        Returns:
            Created typed link.

        Raises:
            AccessDenied: If the scope or a policy rejects an endpoint.
            MemoryConflict: If the relation is not protected, or the edge
                makes a directed cycle. The cycle check includes edges that
                the access object cannot read.
            MemoryNotFound: If an endpoint does not exist.
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
        """Merge metadata into one lineage edge. The topology does not change.

        Args:
            edge: Existing lineage edge to update.
            metadata: Edge property fields to merge.

        Returns:
            Stored replacement link, with the same identity and endpoints.

        Raises:
            MemoryNotFound: If the edge does not exist, or the access object
                cannot see it.
            ValueError: If the edge does not belong to the relation of this
                DAG.
        """

        stored = self._require_stored_edge(edge)
        self._require_relation(stored)
        return self._graph.update_edge(stored, metadata=metadata)

    def edges(
        self,
        block: BlockRef,
        *,
        direction: LinkDirection = "out",
    ) -> list[MemoryLink]:
        """Return visible lineage edges connected to one block.

        Args:
            block: Memory block or id whose edges to return.
            direction: Edge direction relative to the selected block.

        Returns:
            Visible edges in the relation of this DAG, in edge order.

        Raises:
            ValueError: If `direction` is not `"out"`, `"in"`, or `"both"`.
        """

        return self._graph.edges(
            block,
            relation=self.relation,
            direction=direction,
        )

    def predecessors(self, block: BlockRef) -> list[GraphNeighbor]:
        """Return visible direct predecessors that incoming edges connect.

        Args:
            block: Memory block or id whose predecessors to return.

        Returns:
            One neighbor record for each incoming edge, so parallel edges stay
            separate.

        Raises:
            MemoryConflict: If a backend returns an edge that is not connected
                to the selected block.
            MemoryNotFound: If an edge endpoint no longer exists during the
                traversal.
        """

        return self._graph.neighbors(
            block,
            relation=self.relation,
            direction="in",
        )

    def successors(self, block: BlockRef) -> list[GraphNeighbor]:
        """Return visible direct successors that outgoing edges connect.

        Args:
            block: Memory block or id whose successors to return.

        Returns:
            One neighbor record for each outgoing edge, so parallel edges stay
            separate.

        Raises:
            MemoryConflict: If a backend returns an edge that is not connected
                to the selected block.
            MemoryNotFound: If an edge endpoint no longer exists during the
                traversal.
        """

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
        """Return bounded lineage paths that go toward earlier blocks.

        Args:
            block: Start memory block or id.
            max_depth: Maximum number of edges in a returned path.
            limit: Maximum number of paths to return.

        Returns:
            Paths with 1 to `max_depth` edges, in breadth-first order.

        Raises:
            MemoryNotFound: If the start block does not exist.
            TypeError: If `max_depth` or `limit` is not an integer.
            ValueError: If a bound is negative.
        """

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
        """Return bounded lineage paths that go toward derived blocks.

        Args:
            block: Start memory block or id.
            max_depth: Maximum number of edges in a returned path.
            limit: Maximum number of paths to return.

        Returns:
            Paths with 1 to `max_depth` edges, in breadth-first order.

        Raises:
            MemoryNotFound: If the start block does not exist.
            TypeError: If `max_depth` or `limit` is not an integer.
            ValueError: If a bound is negative.
        """

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
        """Return the shortest visible forward lineage path, if one exists.

        Args:
            predecessor: Start memory block or id.
            successor: Target memory block or id.
            max_depth: Maximum number of edges in the path.

        Returns:
            Shortest path, a path with zero edges when both blocks are the
            same, or `None` if no path is inside the `max_depth` limit.

        Raises:
            MemoryNotFound: If an endpoint does not exist.
            TypeError: If `max_depth` is not an integer.
            ValueError: If `max_depth` is negative.
        """

        return self._graph.shortest_path(
            predecessor,
            successor,
            relation=self.relation,
            direction="out",
            max_depth=max_depth,
        )

    def _require_relation(self, edge: MemoryLink) -> None:
        """Raise `ValueError` if an edge does not belong to the relation of this DAG."""

        if edge.relation != self.relation:
            raise ValueError(
                f"Expected lineage relation {self.relation!r}, got "
                f"{edge.relation!r}."
            )

    def _require_stored_edge(self, edge: MemoryLink) -> MemoryLink:
        """Return the stored visible edge, and do not trust the fields from the caller."""

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
    """Raise an error if a relation is not a non-empty string, or is the mount relation."""

    if not isinstance(relation, str):
        raise TypeError("relation must be a string.")
    if not relation.strip():
        raise ValueError("relation must not be empty.")
    if relation == LinkRelations.MOUNT:
        raise ValueError("The reserved mount relation cannot identify a lineage DAG.")
