"""Shared memory access protocols for optional structures.

Structure facades depend on these small protocols instead of a concrete
`Memory` or `MemoryHandle` class. Both public access classes satisfy the
combined `MemoryAccess` protocol. A facade can depend on a narrower protocol
when it needs only part of the memory API.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.policy.engine import Operation
from genie.policy.policy import MemoryPolicy
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult

BlockRef: TypeAlias = str | MemoryBlock
"""A memory block or its stable id."""

LinkDirection: TypeAlias = Literal["out", "in", "both"]
"""Direction used when listing links relative to a block."""


@runtime_checkable
class BlockReader(Protocol):
    """Read access needed by structure facades."""

    def get(self, block_id: BlockRef) -> MemoryBlock | None:
        """Fetch a visible memory block by block or id.

        Args:
            block_id: Block or block id to read.

        Returns:
            Visible block, or `None` when it is missing or not visible.
        """

        ...


@runtime_checkable
class BlockWriter(Protocol):
    """Block mutation access needed by structure facades."""

    def put(
        self,
        *,
        type: str,
        value: Any,
        parent: BlockRef | None = None,
        metadata: dict[str, Any] | None = None,
        policy: MemoryPolicy | None = None,
        content_type: str | None = None,
        id: str | None = None,
    ) -> MemoryBlock:
        """Create a memory block.

        Args:
            type: Application-defined block type.
            value: User-level value to encode and store.
            parent: Optional parent block or id.
            metadata: Optional metadata used for filtering or indexing.
            policy: Optional block-level access policy.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Stored block with a decoded value.
        """

        ...

    def set(
        self,
        block_id: BlockRef,
        value: Any,
        *,
        content_type: str | None = None,
    ) -> MemoryBlock:
        """Replace a memory block's value.

        Args:
            block_id: Block or block id to replace.
            value: Replacement user-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Stored block with a decoded value.
        """

        ...

    def update(
        self,
        block_id: BlockRef,
        *,
        value: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryBlock:
        """Merge fields into a dictionary value or block metadata.

        Args:
            block_id: Block or block id to update.
            value: Optional fields to merge into a dictionary value.
            metadata: Optional fields to merge into the block metadata.

        Returns:
            Stored block with a decoded value.
        """

        ...

    def delete(self, block_id: BlockRef) -> None:
        """Delete a memory block using the access object's normal rules.

        Args:
            block_id: Block or block id to delete, together with everything it
                owns.
        """

        ...


@runtime_checkable
class AccessChecker(Protocol):
    """Policy check access needed by facades that change membership."""

    def require_access(self, operation: Operation | str, block_id: BlockRef) -> None:
        """Require that the acting principal may perform an operation on a block.

        Args:
            operation: Built-in `Operation`, or the name of a custom operation
                configured on the policy engine.
            block_id: Block or block id carrying the policy.

        Raises:
            AccessDenied: If the operation is not allowed.
            MemoryNotFound: If the block does not exist.
        """

        ...


@runtime_checkable
class TreeReader(Protocol):
    """Parent/child traversal access needed by tree-based structures."""

    def children(self, block_id: BlockRef) -> list[MemoryBlock]:
        """Return visible direct children of a memory block.

        Args:
            block_id: Parent block or block id.

        Returns:
            Visible direct child blocks.
        """

        ...

    def ancestors(self, block_id: BlockRef) -> list[MemoryBlock]:
        """Return visible ancestors of a memory block.

        Args:
            block_id: Block or block id to inspect.

        Returns:
            Visible ancestor blocks in nearest-parent-first order.
        """

        ...


@runtime_checkable
class LinkReader(Protocol):
    """Typed-link lookup access needed by graph-based structures."""

    def list_links(
        self,
        block_id: BlockRef,
        *,
        relation: str | None = None,
        direction: LinkDirection = "out",
    ) -> list[MemoryLink]:
        """List visible links connected to a memory block.

        Args:
            block_id: Block or block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Visible links matching the filters.
        """

        ...


@runtime_checkable
class LinkWriter(Protocol):
    """Typed-link mutation access needed by graph-based structures."""

    def add_link(
        self,
        source: BlockRef,
        target: BlockRef,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a typed link between two memory blocks.

        Args:
            source: Block or id the link starts from.
            target: Block or id the link points to.
            relation: Application-defined relationship type.
            metadata: Optional relationship metadata.

        Returns:
            Stored link.
        """

        ...

    def update_link(
        self,
        link: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge metadata into an existing typed link.

        Args:
            link: Link to update.
            metadata: Fields to merge into the link metadata.

        Returns:
            Stored replacement link.
        """

        ...


@runtime_checkable
class AcyclicLinkWriter(Protocol):
    """Atomic acyclic-link mutation access needed by DAG structures."""

    def add_acyclic_link(
        self,
        source: BlockRef,
        target: BlockRef,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Atomically create a typed link unless it would form a cycle.

        Args:
            source: Block or id the link starts from.
            target: Block or id the link points to.
            relation: Relation identifying the protected acyclic graph.
            metadata: Optional relationship metadata.

        Returns:
            Stored link.
        """

        ...


@runtime_checkable
class SearchAccess(Protocol):
    """Search access needed by structures with portable query fallbacks."""

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Run a normalized memory query through the access boundary.

        Args:
            query: Normalized memory query.

        Returns:
            Visible search results.
        """

        ...


@runtime_checkable
class MemoryAccess(
    BlockReader,
    BlockWriter,
    AccessChecker,
    TreeReader,
    LinkReader,
    LinkWriter,
    AcyclicLinkWriter,
    SearchAccess,
    Protocol,
):
    """Combined structural interface implemented by `Memory` and `MemoryHandle`."""
