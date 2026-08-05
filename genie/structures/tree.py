"""Rooted facade for Genie's parent/child ownership tree."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict, MemoryNotFound
from genie.policy.engine import AccessDenied
from genie.policy.policy import MemoryPolicy
from genie.structures.access import BlockReader, BlockRef, TreeReader


@runtime_checkable
class OwnershipAccess(BlockReader, TreeReader, Protocol):
    """Small access surface required by ``OwnershipTree``."""

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
        """Create a memory block."""

        ...

    def delete(self, block_id: BlockRef) -> None:
        """Delete a memory block and its owned descendants."""

        ...


class OwnershipTree:
    """A rooted view over existing parent/child ownership operations.

    The facade adds no storage behavior. It keeps every operation inside its
    configured root and delegates reads, writes, traversal, policy checks, and
    recursive deletion to the supplied ``Memory`` or ``MemoryHandle`` access
    object.
    """

    __slots__ = ("_access", "root_id")

    def __init__(self, access: OwnershipAccess, *, root: BlockRef) -> None:
        """Create an ownership-tree view rooted at a visible block.

        Args:
            access: Full or scoped memory access used for every operation.
            root: Block or id that bounds this tree.

        Raises:
            AccessDenied: If a scoped access object cannot read the root.
            MemoryNotFound: If the root does not exist.
        """

        stored_root = access.get(root)
        if stored_root is None:
            raise MemoryNotFound(f"Ownership tree root {_block_id(root)!r} does not exist.")
        self._access = access
        self.root_id = stored_root.id

    @property
    def root(self) -> MemoryBlock:
        """Return the current root block.

        Returns:
            Current visible root block.

        Raises:
            AccessDenied: If the root is no longer readable.
            MemoryNotFound: If the root was deleted.
        """

        root = self._access.get(self.root_id)
        if root is None:
            raise MemoryNotFound(f"Ownership tree root {self.root_id!r} does not exist.")
        return root

    def create_child(
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
        """Create a child under this tree's root or another visible member.

        Args:
            type: Application-defined block type.
            value: User-level block value.
            parent: Optional parent in this tree. Defaults to the tree root.
            metadata: Optional block metadata.
            policy: Optional block policy, subject to the access object's rules.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Created memory block.

        Raises:
            AccessDenied: If the parent is outside the tree or normal access
                checks reject the write.
            MemoryNotFound: If the tree root no longer exists.
        """

        selected_parent = self.root if parent is None else self._require_member(parent)
        return self._access.put(
            type=type,
            value=value,
            parent=selected_parent,
            metadata=metadata,
            policy=policy,
            content_type=content_type,
            id=id,
        )

    def children(self, block: BlockRef | None = None) -> list[MemoryBlock]:
        """Return visible direct children of a tree member.

        Args:
            block: Optional member whose children should be returned. Defaults
                to the tree root.

        Returns:
            Visible direct children in the access object's order.

        Raises:
            AccessDenied: If the block is outside this tree or traversal is
                denied.
            MemoryNotFound: If the tree root no longer exists.
        """

        selected = self.root if block is None else self._require_member(block)
        return self._access.children(selected)

    def parent(self, block: BlockRef) -> MemoryBlock | None:
        """Return a visible direct parent inside this tree.

        The root has no parent in this facade, even when it has an owner above
        the facade boundary.

        Args:
            block: Tree member whose parent should be returned.

        Returns:
            Visible direct parent, or ``None`` for the facade root.

        Raises:
            AccessDenied: If the block is outside this tree or upward traversal
                is disabled or denied.
        """

        member = self._require_member(block)
        if member.id == self.root_id or member.parent_id is None:
            return None
        return next(
            (
                ancestor
                for ancestor in self._bounded_ancestors(member)
                if ancestor.id == member.parent_id
            ),
            None,
        )

    def ancestors(
        self,
        block: BlockRef,
        *,
        include_self: bool = False,
    ) -> list[MemoryBlock]:
        """Return visible ancestors without crossing above the tree root.

        Args:
            block: Tree member whose ancestors should be returned.
            include_self: Whether to place the selected block first.

        Returns:
            Visible blocks in nearest-first order, ending at the tree root.

        Raises:
            AccessDenied: If the block is outside this tree or upward traversal
                is disabled or denied.
        """

        member = self._require_member(block)
        ancestors = self._bounded_ancestors(member)
        return [member, *ancestors] if include_self else ancestors

    def descendants(
        self,
        block: BlockRef | None = None,
        *,
        include_self: bool = False,
    ) -> list[MemoryBlock]:
        """Return visible descendants in depth-first pre-order.

        Args:
            block: Optional tree member to use as the traversal root. Defaults
                to this facade's root.
            include_self: Whether to include the selected traversal root.

        Returns:
            Visible descendants in deterministic depth-first pre-order, using
            the child order returned by the access object.

        Raises:
            AccessDenied: If the block is outside this tree or downward
                traversal is denied.
            MemoryConflict: If backend data repeats a block inside the tree.
        """

        selected = self.root if block is None else self._require_member(block)
        return list(self._walk(selected, include_self=include_self))

    def contains(self, block: BlockRef) -> bool:
        """Return whether a block id is visible inside this tree.

        Missing, unreadable, and out-of-tree block ids return ``False`` unless
        traversal itself is denied. The check does not cross links or mounts.

        Args:
            block: Candidate block or id.

        Returns:
            ``True`` when the candidate is the root or a visible descendant.

        Raises:
            AccessDenied: If downward traversal needed for the check is denied.
            MemoryNotFound: If the tree root no longer exists.
        """

        candidate_id = _block_id(block)
        return any(
            member.id == candidate_id
            for member in self._walk(self.root, include_self=True)
        )

    def delete_subtree(self, block: BlockRef) -> None:
        """Delete a tree member and its owned descendants.

        The target is required so deleting the whole facade root is explicit.
        Normal recursive-delete policy checks remain in the access object.

        Args:
            block: Tree member to delete.

        Raises:
            AccessDenied: If the block is outside this tree or deletion is
                denied for any affected block.
            MemoryNotFound: If the tree root no longer exists.
        """

        member = self._require_member(block)
        self._access.delete(member)

    def _require_member(self, block: BlockRef) -> MemoryBlock:
        """Return a visible member or reject an escape from the facade root."""

        block_id = _block_id(block)
        for member in self._walk(self.root, include_self=True):
            if member.id == block_id:
                return member
        raise AccessDenied(
            f"Memory block {block_id!r} is outside ownership tree root {self.root_id!r}."
        )

    def _bounded_ancestors(self, member: MemoryBlock) -> list[MemoryBlock]:
        """Return visible ancestors through this facade's root and no higher."""

        if member.id == self.root_id:
            return []
        ancestors: list[MemoryBlock] = []
        for ancestor in self._access.ancestors(member):
            ancestors.append(ancestor)
            if ancestor.id == self.root_id:
                return ancestors
        raise AccessDenied(
            f"Cannot traverse from memory block {member.id!r} to ownership tree "
            f"root {self.root_id!r}."
        )

    def _walk(
        self,
        start: MemoryBlock,
        *,
        include_self: bool,
    ) -> Iterator[MemoryBlock]:
        """Yield a checked depth-first pre-order traversal from one member."""

        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current.id in seen:
                raise MemoryConflict(
                    f"Ownership tree contains repeated memory block {current.id!r}."
                )
            seen.add(current.id)
            if include_self or current.id != start.id:
                yield current
            stack.extend(reversed(self._access.children(current)))


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block
