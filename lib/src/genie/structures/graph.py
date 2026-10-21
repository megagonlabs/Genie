"""Property-graph facade over the existing blocks and typed links of Genie."""

from __future__ import annotations

from collections import deque
from typing import Any, Literal, Protocol, runtime_checkable

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
    """Small access surface that `PropertyGraph` requires."""


class PropertyGraph:
    """Property graph that uses blocks as nodes and links as edges, through an access object.

    - **Traversal only:** the facade adds graph traversal, but it stores nothing
      of its own.
    - **Access object does the work:** edge creation, metadata updates, edge
      lookup, block reads, policy checks, and handle scope checks stay in the
      `Memory` or `MemoryHandle` object.
    """

    __slots__ = ("_access",)

    def __init__(self, access: GraphAccess) -> None:
        """Create a property-graph view over a memory access object.

        Args:
            access: Full or scoped memory access for all graph operations.
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
            AccessDenied: If the scope or a policy rejects an endpoint.
            MemoryNotFound: If an endpoint does not exist.
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
        """Merge properties into an existing edge. The topology does not change.

        Args:
            edge: Existing memory link.
            metadata: Edge property fields to merge.

        Returns:
            Stored replacement link, with the same identity and endpoints.
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
            block: Memory block or id to examine.
            relation: Optional exact relation filter.
            direction: Edge direction relative to the selected node.

        Returns:
            Visible memory links that match, in the order of the access object.

        Raises:
            ValueError: If `direction` is not `"out"`, `"in"`, or `"both"`.
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
        """Return visible neighbor nodes, with the edges that reach them.

        - **One record for each edge:** so parallel edges stay separate.
        - **Self-links:** with `direction="both"`, a self-link is reported as
          outgoing.

        Args:
            block: Memory block or id whose neighbors to return.
            relation: Optional exact relation filter.
            direction: Edge direction relative to the selected node.

        Returns:
            Neighbor records in edge order.

        Raises:
            MemoryConflict: If a backend returns an edge that is not connected
                to the selected node.
            MemoryNotFound: If an edge endpoint no longer exists during the
                traversal.
            ValueError: If `direction` is not `"out"`, `"in"`, or `"both"`.
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
        """Return bounded simple paths from a visible start node.

        - **Order:** breadth-first, so shorter paths come first.
        - **No repeated block in a path:** so a cycle cannot make the traversal
          infinite. The graph itself can still contain cycles.
        - **Parallel edges:** each one gives a separate path.

        Args:
            start: Start memory block or id.
            relation: Optional exact relation filter for each step.
            direction: Edge direction to follow at each step.
            max_depth: Maximum number of edges in a returned path.
            limit: Maximum number of paths to return.

        Returns:
            Paths with 1 to `max_depth` edges, in breadth-first order.
            `max_depth=0` or `limit=0` gives an empty list.

        Raises:
            MemoryNotFound: If the start block does not exist.
            TypeError: If `max_depth` or `limit` is not an integer.
            ValueError: If a bound is negative or `direction` is not valid.
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
        """Return the shortest visible path between 2 nodes.

        - **First shortest path:** a breadth-first traversal returns the first
          shortest path, in the edge order of the access object.
        - **Depth limit:** the search always stops at `max_depth`.

        Args:
            start: Start memory block or id.
            target: Target memory block or id.
            relation: Optional exact relation filter for each step.
            direction: Edge direction to follow at each step.
            max_depth: Maximum number of edges in the path.

        Returns:
            Shortest path, a path with zero edges when both nodes are the same,
            or `None` if no path is inside the `max_depth` limit.

        Raises:
            MemoryNotFound: If an endpoint does not exist.
            TypeError: If `max_depth` is not an integer.
            ValueError: If `max_depth` is negative or `direction` is not valid.
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
        """Resolve one edge to a visible neighbor relative to `block_id`."""

        if edge.source_id == block_id and edge.target_id == block_id:
            direction: Literal["out", "in"] = (
                "in" if requested_direction == "in" else "out"
            )
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
        """Return a visible node, or raise `MemoryNotFound` with a clear message."""

        stored = self._access.get(block)
        if stored is None:
            raise MemoryNotFound(f"Graph node {_block_id(block)!r} does not exist.")
        return stored


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block


def _validate_bound(value: object, *, name: str) -> None:
    """Raise an error if a traversal bound is not a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_direction(value: object) -> None:
    """Raise `ValueError` if a link traversal direction is not supported."""

    if value not in {"out", "in", "both"}:
        raise ValueError("direction must be one of 'out', 'in', or 'both'.")
