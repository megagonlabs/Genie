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

        Deletion covers the block and every descendant. An adapter must also
        remove the dependent state it owns for those blocks: links in either
        direction, key bindings, sequence entries, and temporal entries. No
        link may survive pointing at a removed block, whether the removed block
        was its source or its target.

        `Memory` does not perform this cleanup and never calls the explicit
        `delete_*_for_block` methods, so an adapter that skips it here leaves
        dangling relationships behind. How an adapter satisfies this is its own
        choice: the explicit cleanup methods, a cascading foreign key, deleting
        the dependent records in the same transaction, or staging every
        dependent removal in the same commit. What matters is that the cleanup
        lands atomically with the deletion.

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
    """Read-only parent/child traversal interface.

    Ownership is recorded by `MemoryBlock.parent_id` in the block store, so a
    traversal index is a derived view and has no write methods. An adapter that
    keeps blocks and topology in one database can answer these methods directly
    from stored parents.

    `Memory` resolves every ownership question through this interface and never
    reads `parent_id` itself. A separately supplied traversal index therefore
    decides subtree scope, traversal, and recursive deletion, and must stay
    consistent with the block store. An index that omits descendants makes
    `Memory.delete` send an incomplete subtree snapshot, which a correct
    `BlockStore` rejects with `MemoryConflict`.
    """

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
    """Read-only search interface for backend adapters.

    A search index is a derived view and has no write methods. It is derived
    from block state and from link state, because `Scope.LINKS` is resolved
    from stored links rather than from ownership.
    """

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search memory blocks.

        The index resolves `query.scope` against `query.root_id` before it
        ranks anything. An index that holds neither ownership nor link state
        needs access to a `TraversalIndex` and a `LinkStore` to do that.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results. The public `Memory` layer applies policy filtering
            and reapplies the query scope after backend search, so returning
            out-of-scope blocks is safe but spends `query.limit` on results the
            caller never receives.
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

        Adapters must remove both incoming and outgoing links for the block.

        `Memory.delete` does not call this method. Removing the links of a
        deleted subtree is part of `delete_block`, which may use this method,
        a cascading foreign key, or any other means. This method exists for
        adapters and callers that need the cleanup on its own.

        Args:
            block_id: Block id whose connected links should be removed. A
                missing id is ignored.
        """

        ...
