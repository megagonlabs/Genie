"""Smaller backend interface protocols.

- **One backend:** the public `Memory` API accepts a single `MemoryBackend`.
- **Smaller pieces:** the interface has smaller parts, so adapter authors can
  compose storage, traversal, search, and link implementations internally.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


@runtime_checkable
class BlockStore(Protocol):
    """Storage interface for memory blocks."""

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new block.

        Args:
            block: Encoded block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists or the parent is invalid.
        """

        ...

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Get a block by id.

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
        """Replace an existing block, with an optional compare-and-swap.

        - **Fixed identity:** the replacement keeps the `type`, `created_at`,
          `created_by`, and `policy` of the stored block. These fields do not
          change during the life of a block.
        - **Same id, new block:** a block that is deleted and then created
          again with the same id is a different block. A replacement made from
          the old block fails, also when the versions are equal.

        Args:
            block: Encoded replacement block.
            expected_version: Expected current version. With a value, the
                contract requires a replacement version of
                `expected_version + 1`. The write fails if another writer
                changed the block first.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block does not exist, its version changed,
                the replacement changes its `type`, `created_at`,
                `created_by`, or `policy`, or the change breaks a structure
                rule.
        """

        ...

    def delete_block(
        self,
        block_id: str,
        *,
        expected_blocks: Sequence[MemoryBlock] | None = None,
    ) -> None:
        """Delete a block and backend-owned dependent state.

        - **Scope:** the delete covers the block and all its descendants.
        - **Dependent state:** the contract requires the adapter to also remove
          the dependent state that it owns for those blocks. That is links in
          either direction, key bindings, sequence entries, and temporal
          entries. No link stays that points at a removed block, as its source
          or as its target.
        - **No cleanup in `Memory`:** `Memory` does not do this cleanup, and it
          never calls the explicit `delete_*_for_block` methods. So an adapter
          that skips the cleanup here leaves dangling relationships.
        - **Method:** the adapter chooses how to do it. Examples are the
          explicit cleanup methods, a cascading foreign key, a delete of the
          dependent records in the same transaction, or each dependent removal
          staged in the same commit. The cleanup and the delete are atomic
          together.

        Args:
            block_id: Memory block id.
            expected_blocks: Exact snapshot of the subtree to delete, as the
                blocks that the caller read. With a value, the delete is
                atomic. It fails if the subtree membership changed, or if the
                `version`, `type`, `created_at`, `created_by`, or `policy` of a
                block changed. So a block that is deleted and then created
                again with the same id and version does not match.

        Raises:
            MemoryConflict: If `expected_blocks` no longer describes the
                complete subtree.
        """

        ...


@runtime_checkable
class TraversalIndex(Protocol):
    """Read-only parent/child traversal interface.

    - **Derived view:** the block store records ownership in
      `MemoryBlock.parent_id`. So a traversal index is a derived view, and it
      has no write methods.
    - **One database:** an adapter that keeps blocks and topology in one
      database can answer these methods directly from stored parents.
    - **Ownership source:** `Memory` answers each ownership question through
      this interface, and it never reads `parent_id` itself.
    - **Separate index:** so a separate traversal index decides subtree scope,
      traversal, and recursive delete. It has to agree with the block store.
    - **Incomplete index:** an index that leaves out descendants makes
      `Memory.delete` send an incomplete subtree snapshot. A correct
      `BlockStore` rejects it with `MemoryConflict`.
    """

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return the direct children of a block id.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks.
        """

        ...

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from nearest parent upward.

        Args:
            block_id: Block id to examine.

        Returns:
            Ancestor blocks, nearest parent first.
        """

        ...

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return all descendants of a block id.

        Args:
            block_id: Root block id.
            include_self: If `True`, the result includes `block_id`.

        Returns:
            Descendant blocks in backend-defined traversal order.
        """

        ...

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is inside the subtree of `root_id`.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when `block_id` is `root_id` or is below it.
        """

        ...


@runtime_checkable
class SearchIndex(Protocol):
    """Read-only search interface for backend adapters.

    - **Derived view:** a search index is a derived view, and it has no write
      methods.
    - **Sources:** it comes from block state and from link state, because
      `Scope.LINKS` resolves from stored links, not from ownership.
    """

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search memory blocks.

        - **Scope first:** the index resolves `query.scope` against
          `query.root_id` before it ranks anything.
        - **No state of its own:** an index that holds neither ownership nor
          link state needs access to a `TraversalIndex` and a `LinkStore` for
          this.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results. After the backend search, the public `Memory` layer
            applies the policy filter and the query scope again. So a block
            outside the scope is safe to return, but it uses part of
            `query.limit` for a result that the caller never gets.
        """

        ...


@runtime_checkable
class LinkStore(Protocol):
    """Storage and lookup interface for typed memory links."""

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a new typed link.

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

        The contract requires that the replacement keeps the link id, source,
        target, relation, and creation timestamp.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link does not exist, or a field other than
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
            block_id: Block id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            ValueError: If `direction` is not valid.
        """

        ...

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete links connected to a block.

        - **Both directions:** the contract requires the adapter to remove the
          incoming and the outgoing links of the block.
        - **Not called by `Memory.delete`:** the removal of the links of a
          deleted subtree is part of `delete_block`. That method can use this
          method, a cascading foreign key, or any other means.
        - **Use:** this method is for adapters and callers that need the
          cleanup alone.

        Args:
            block_id: Block id whose connected links to remove. An unknown id
                is ignored.
        """

        ...
