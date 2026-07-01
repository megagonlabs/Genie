"""Backend capability protocols.

The public `Memory` API accepts a single `MemoryBackend`, but the contract is
split into capabilities so adapter authors can compose storage, traversal,
search, and link implementations internally.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


@runtime_checkable
class BlockStore(Protocol):
    """Persistence contract for memory blocks."""

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Persist a new block.

        Args:
            block: Encoded block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists or the parent is invalid.
        """

        ...

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored block, or `None` when it does not exist.
        """

        ...

    def update_block(self, block: MemoryBlock) -> MemoryBlock:
        """Replace an existing block.

        Args:
            block: Encoded replacement block.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block is missing or structural constraints
                would be violated.
        """

        ...

    def delete_block(self, block_id: str) -> None:
        """Delete a block and backend-owned dependent state.

        Args:
            block_id: Memory block id.
        """

        ...


@runtime_checkable
class TraversalIndex(Protocol):
    """Hierarchy traversal contract."""

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children for a block id.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks.
        """

        ...

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks in nearest-parent-first order.
        """

        ...

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return all descendants for a block id.

        Args:
            block_id: Root block id.
            include_self: Whether to include `block_id` in the result.

        Returns:
            Descendant blocks in backend-defined traversal order.
        """

        ...

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is inside `root_id`'s subtree.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when `block_id` is equal to or below `root_id`.
        """

        ...


@runtime_checkable
class SearchIndex(Protocol):
    """Search contract for backend adapters."""

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search memory blocks.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results. The public `Memory` layer applies policy filtering
            after backend search.
        """

        ...


@runtime_checkable
class LinkStore(Protocol):
    """Persistence and lookup contract for typed memory links."""

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Persist a new typed link.

        Args:
            link: Link to store.

        Returns:
            Stored link.
        """

        ...

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            ValueError: If `direction` is invalid.
        """

        ...

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete links connected to a block.

        Backend adapters should remove both incoming and outgoing links for the
        block. `Memory.delete` uses this capability to keep link indexes from
        retaining dangling relationships after recursive deletes.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        ...
