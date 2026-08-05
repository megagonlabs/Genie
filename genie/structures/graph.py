"""Property-graph facade over Genie's existing blocks and typed links."""

from __future__ import annotations

from collections import deque
from typing import Any, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict, MemoryNotFound
from genie.memory.links import MemoryLink
from genie.structures.access import (
    BlockReader,
    BlockRef,
    LinkDirection,
    LinkReader,
    LinkWriter,
)
from genie.structures.records import GraphNeighbor, GraphPath


@runtime_checkable
class GraphAccess(BlockReader, LinkReader, LinkWriter, Protocol):
    """Small access surface required by ``PropertyGraph``."""


class PropertyGraph:
    """Access-aware property graph using blocks as nodes and links as edges.

    The facade adds graph traversal but no graph persistence. Edge creation,
    metadata updates, edge lookup, block reads, policy checks, and handle scope
    checks remain in the supplied ``Memory`` or ``MemoryHandle`` object.
    """

    __slots__ = ("_access",)

    def __init__(self, access: GraphAccess) -> None:
        """Create a property-graph view over a memory access object.

        Args:
            access: Full or scoped memory access used for every graph operation.
        """

        self._access = access

    def add_edge(
        self,
        source: BlockRef,
        target: BlockRef,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a directed typed edge between two visible nodes.

        Args:
            source: Source memory block or id.
            target: Target memory block or id.
            relation: Application-defined edge type.
            metadata: Optional edge properties.

        Returns:
            Created memory link.

        Raises:
            AccessDenied: If scope or policy rejects either endpoint.
            MemoryNotFound: If either endpoint is missing.
        """

        return self._access.add_link(
            source,
            target,
            relation,
            metadata=metadata,
        )

    def update_edge(
        self,
        edge: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge properties into an existing edge without changing topology.

        Args:
            edge: Existing memory link.
            metadata: Edge property fields to merge.

        Returns:
            Persisted replacement link with stable identity and endpoints.
        """

        return self._access.update_link(edge, metadata=metadata)

    def edges(
        self,
        block: BlockRef,
        *,
        relation: str | None = None,
        direction: LinkDirection = "out",
    ) -> list[MemoryLink]:
        """Return visible edges connected to one node.

        Args:
            block: Memory block or id to inspect.
            relation: Optional exact relation filter.
            direction: Edge direction relative to the selected node.

        Returns:
            Matching visible memory links in the access object's order.
        """

        _validate_direction(direction)
        return self._access.list_links(
            block,
            relation=relation,
            direction=direction,
        )

    def neighbors(
        self,
        block: BlockRef,
        *,
        relation: str | None = None,
        direction: LinkDirection = "out",
    ) -> list[GraphNeighbor]:
        """Return visible neighboring nodes with the edges used to reach them.

        One record is returned per edge, so parallel edges remain distinct.
        Self-links are reported as outgoing when ``direction="both"``.

        Args:
            block: Memory block or id whose neighbors should be returned.
            relation: Optional exact relation filter.
            direction: Edge direction relative to the selected node.

        Returns:
            Neighbor records in edge order.

        Raises:
            MemoryConflict: If a backend returns an edge that is not connected
                to the selected node.
            MemoryNotFound: If an edge endpoint disappears during traversal.
        """

        block_id = _block_id(block)
        _validate_direction(direction)
        return [
            self._neighbor(block_id, edge, requested_direction=direction)
            for edge in self.edges(block, relation=relation, direction=direction)
        ]

    def walk(
        self,
        start: BlockRef,
        *,
        relation: str | None = None,
        direction: LinkDirection = "out",
        max_depth: int = 1,
        limit: int = 100,
    ) -> list[GraphPath]:
        """Return bounded simple paths from a visible starting node.

        Paths are returned in breadth-first order, so shorter paths appear
        first. A block is not repeated inside one path; this prevents cycles
        from making traversal infinite while allowing the graph itself to
        contain cycles. Parallel edges produce separate paths.

        Args:
            start: Starting memory block or id.
            relation: Optional exact relation filter applied at every step.
            direction: Edge direction followed at every step.
            max_depth: Maximum number of edges in a returned path.
            limit: Maximum number of paths to return.

        Returns:
            Paths of length one through ``max_depth`` in breadth-first order.

        Raises:
            MemoryNotFound: If the starting block is missing.
            TypeError: If ``max_depth`` or ``limit`` is not an integer.
            ValueError: If either bound is negative or direction is invalid.
        """

        _validate_bound(max_depth, name="max_depth")
        _validate_bound(limit, name="limit")
        _validate_direction(direction)
        start_block = self._require_node(start)
        if max_depth == 0 or limit == 0:
            return []

        paths: list[GraphPath] = []
        pending = deque([GraphPath(block_ids=(start_block.id,))])
        while pending and len(paths) < limit:
            path = pending.popleft()
            if len(path) >= max_depth:
                continue
            for neighbor in self.neighbors(
                path.end_id,
                relation=relation,
                direction=direction,
            ):
                if neighbor.block.id in path.block_ids:
                    continue
                next_path = GraphPath(
                    block_ids=(*path.block_ids, neighbor.block.id),
                    links=(*path.links, neighbor.link),
                )
                paths.append(next_path)
                if len(paths) >= limit:
                    break
                pending.append(next_path)
        return paths

    def shortest_path(
        self,
        start: BlockRef,
        target: BlockRef,
        *,
        relation: str | None = None,
        direction: LinkDirection = "out",
        max_depth: int = 6,
    ) -> GraphPath | None:
        """Return the shortest visible path between two nodes.

        Breadth-first traversal returns the first shortest path using the edge
        order supplied by the access object. The search is always depth-bounded.

        Args:
            start: Starting memory block or id.
            target: Target memory block or id.
            relation: Optional exact relation filter applied at every step.
            direction: Edge direction followed at every step.
            max_depth: Maximum number of edges to inspect.

        Returns:
            Shortest path, a zero-edge path when both nodes are the same, or
            ``None`` when no path is found within ``max_depth``.

        Raises:
            MemoryNotFound: If either endpoint is missing.
            TypeError: If ``max_depth`` is not an integer.
            ValueError: If ``max_depth`` is negative or direction is invalid.
        """

        _validate_bound(max_depth, name="max_depth")
        _validate_direction(direction)
        start_block = self._require_node(start)
        target_block = self._require_node(target)
        initial = GraphPath(block_ids=(start_block.id,))
        if start_block.id == target_block.id:
            return initial
        if max_depth == 0:
            return None

        pending = deque([initial])
        visited = {start_block.id}
        while pending:
            path = pending.popleft()
            if len(path) >= max_depth:
                continue
            for neighbor in self.neighbors(
                path.end_id,
                relation=relation,
                direction=direction,
            ):
                if neighbor.block.id in visited:
                    continue
                next_path = GraphPath(
                    block_ids=(*path.block_ids, neighbor.block.id),
                    links=(*path.links, neighbor.link),
                )
                if neighbor.block.id == target_block.id:
                    return next_path
                visited.add(neighbor.block.id)
                pending.append(next_path)
        return None

    def _neighbor(
        self,
        block_id: str,
        edge: MemoryLink,
        *,
        requested_direction: LinkDirection,
    ) -> GraphNeighbor:
        """Resolve one edge to a visible neighbor relative to ``block_id``."""

        if edge.source_id == block_id and edge.target_id == block_id:
            direction = "in" if requested_direction == "in" else "out"
            neighbor_id = block_id
        elif edge.source_id == block_id:
            direction = "out"
            neighbor_id = edge.target_id
        elif edge.target_id == block_id:
            direction = "in"
            neighbor_id = edge.source_id
        else:
            raise MemoryConflict(
                f"Graph edge {edge.id!r} is not connected to memory block "
                f"{block_id!r}."
            )
        neighbor = self._access.get(neighbor_id)
        if neighbor is None:
            raise MemoryNotFound(
                f"Graph edge {edge.id!r} references missing memory block "
                f"{neighbor_id!r}."
            )
        return GraphNeighbor(block=neighbor, link=edge, direction=direction)

    def _require_node(self, block: BlockRef) -> MemoryBlock:
        """Return a visible node or raise a clear missing-node error."""

        stored = self._access.get(block)
        if stored is None:
            raise MemoryNotFound(f"Graph node {_block_id(block)!r} does not exist.")
        return stored


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block


def _validate_bound(value: object, *, name: str) -> None:
    """Require a non-negative integer traversal bound."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_direction(value: object) -> None:
    """Require a supported link traversal direction."""

    if value not in {"out", "in", "both"}:
        raise ValueError("direction must be one of 'out', 'in', or 'both'.")
