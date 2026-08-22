"""Smaller backend interface protocols.

The public `Memory` API accepts a single `MemoryBackend`, but the interface is
split into smaller pieces so adapter authors can compose storage, traversal,
search, and link implementations internally.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


@runtime_checkable
class BlockStore(Protocol):
    """Persistence interface for memory blocks."""

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

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace an existing block, optionally using compare-and-swap.

        Args:
            block: Encoded replacement block.
            expected_version: Required current version. When supplied, the
                replacement must have version `expected_version + 1` and the
                write must fail if another writer changed the block first.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block is missing, its version changed, or
                structural constraints would be violated.
        """

        ...

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a block and backend-owned dependent state.

        Args:
            block_id: Memory block id.
            expected_versions: Exact id-to-version snapshot of the subtree to
                delete. When supplied, deletion must be atomic and fail if the
                subtree membership or any version changed.

        Raises:
            MemoryConflict: If `expected_versions` no longer describes the
                complete subtree.
        """

        ...


@runtime_checkable
class TraversalIndex(Protocol):
    """Parent/child traversal interface."""

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
    """Search interface for backend adapters."""

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
    """Persistence and lookup interface for typed memory links."""

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Persist a new typed link.

        Args:
            link: Link to store.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists.
        """

        ...

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of an existing typed link.

        The replacement must preserve the link id, source, target, relation,
        and creation timestamp.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
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
        block. `Memory.delete` uses this interface to keep link indexes from
        retaining dangling relationships after recursive deletes.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        ...
