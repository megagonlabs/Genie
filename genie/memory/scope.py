"""Scoped memory handle for delegated agent access."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, overload

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.policy.engine import AccessDenied
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

if TYPE_CHECKING:
    from genie.memory.memory import Memory


class MemoryScope:
    """Capability-bearing view of part of a memory system.

    A scope narrows a `Memory` instance by root, principal, and traversal
    permissions. Sub-scopes may narrow authority further but cannot widen it.
    """

    def __init__(
        self,
        *,
        memory: "Memory",
        root_id: str | None,
        principal: Principal,
        default_scope: Scope = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
    ) -> None:
        """Create a scoped memory handle.

        Args:
            memory: Underlying full memory system.
            root_id: Optional root block id. `None` means rootless.
            principal: Principal for operations through this scope.
            default_scope: Search scope used when callers omit `scope`.
            allow_up: Whether ancestor traversal/search is allowed.
            allow_global: Whether global search is allowed.
        """

        self._memory = memory
        self.root_id = root_id
        self.principal = principal
        self.default_scope = default_scope
        self.allow_up = allow_up
        self.allow_global = allow_global

    def put(
        self,
        *,
        type: str,
        value: Any,
        parent: str | MemoryBlock | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MemoryBlock:
        """Create a block inside this scope.

        If `parent` is omitted, the scope root is used as the parent.

        Args:
            type: Application-defined block type.
            value: User-level value.
            parent: Optional parent block or id inside the scope.
            metadata: Optional metadata.
            **kwargs: Additional arguments forwarded to `Memory.put`.

        Returns:
            Created block.

        Raises:
            AccessDenied: If the parent is outside the scope or policy denies
                the write.
        """

        parent_id = _block_id(parent) if parent is not None else self.root_id
        if parent_id is not None:
            self._require_in_subtree(parent_id)
        return self._as_principal().put(
            type=type,
            value=value,
            parent=parent_id,
            metadata=metadata,
            **kwargs,
        )

    def get(self, block_id: str | MemoryBlock) -> MemoryBlock | None:
        """Fetch a block inside this scope.

        Args:
            block_id: Block or id to fetch.

        Returns:
            Decoded block, or `None` when missing.

        Raises:
            AccessDenied: If the block is outside the scope or policy denies
                read access.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().get(actual_id)

    def set(self, block_id: str | MemoryBlock, value: Any, **kwargs: Any) -> MemoryBlock:
        """Replace a block value inside this scope.

        Args:
            block_id: Block or id to update.
            value: New user-level value.
            **kwargs: Additional arguments forwarded to `Memory.set`.

        Returns:
            Updated block.

        Raises:
            AccessDenied: If the block is outside the scope or policy denies
                write access.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().set(actual_id, value, **kwargs)

    def patch(self, block_id: str | MemoryBlock, **kwargs: Any) -> MemoryBlock:
        """Patch a block inside this scope.

        Args:
            block_id: Block or id to patch.
            **kwargs: Patch arguments forwarded to `Memory.patch`.

        Returns:
            Updated block.

        Raises:
            AccessDenied: If the block is outside the scope or policy denies
                write access.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().patch(actual_id, **kwargs)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block inside this scope.

        Args:
            block_id: Block or id to delete.

        Raises:
            AccessDenied: If the block is outside the scope or policy denies
                write access for any affected block.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().delete(actual_id)

    def children(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        """Return children for a block inside this scope.

        Args:
            block_id: Optional block or id. Defaults to the scope root.

        Returns:
            Readable child blocks.

        Raises:
            AccessDenied: If the scope is rootless and no block is provided, or
                if the block is outside the scope.
        """

        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            raise AccessDenied("A rootless memory scope cannot infer which children to fetch.")
        self._require_in_subtree(actual_id)
        return self._as_principal().children(actual_id)

    def ancestors(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        """Return ancestors when this scope allows upward traversal.

        Args:
            block_id: Optional block or id. Defaults to the scope root.

        Returns:
            Readable ancestor blocks.

        Raises:
            AccessDenied: If ancestor traversal is disabled, the scope is
                rootless and no block is provided, or the block is outside the
                scope.
        """

        if not self.allow_up:
            raise AccessDenied("This memory scope does not allow ancestor traversal.")
        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            raise AccessDenied("A rootless memory scope cannot infer which ancestors to fetch.")
        self._require_in_subtree(actual_id)
        return self._as_principal().ancestors(actual_id)

    def add_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a link between two blocks inside this scope.

        Args:
            source: Source block or id inside the scope.
            target: Target block or id inside the scope.
            relation: Application-defined relation type.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If either endpoint is outside the scope or policy
                denies the operation.
        """

        self._require_in_subtree(_block_id(source))
        self._require_in_subtree(_block_id(target))
        return self._as_principal().add_link(source, target, relation, metadata=metadata)

    def list_links(
        self,
        block_id: str | MemoryBlock,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block inside this scope.

        Links whose other endpoint falls outside the scope are hidden.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        links = self._as_principal().list_links(actual_id, relation=relation, direction=direction)
        if self.root_id is None:
            return links
        return [
            link
            for link in links
            if self._memory.contains(self.root_id, link.source_id)
            and self._memory.contains(self.root_id, link.target_id)
        ]

    def search(
        self,
        query: MemoryQuery | str | None = None,
        *,
        scope: Scope | str | None = None,
        root: str | MemoryBlock | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Search through this scope.

        Args:
            query: Optional `MemoryQuery` or text query string.
            scope: Optional search scope overriding the scope default.
            root: Optional root inside this scope.
            **kwargs: Additional arguments forwarded to `Memory.search`.

        Returns:
            Search results visible through the scope.

        Raises:
            AccessDenied: If the requested root or search mode would escape the
                scope's authority.
        """

        if isinstance(query, MemoryQuery):
            selected_scope = Scope(scope or query.scope)
            root_id = _block_id(root) if root is not None else query.root_id or self.root_id
            query = replace(query, root_id=root_id, scope=selected_scope)
        else:
            selected_scope = Scope(scope or self.default_scope)
            root_id = _block_id(root) if root is not None else self.root_id
        if selected_scope == Scope.GLOBAL and not self.allow_global:
            raise AccessDenied("This memory scope does not allow global search.")
        if selected_scope == Scope.ANCESTORS and not self.allow_up:
            raise AccessDenied("This memory scope does not allow ancestor search.")
        if root_id is not None and selected_scope != Scope.GLOBAL:
            self._require_in_subtree(root_id)
        # Rebuild query objects with the selected scope/root so a caller cannot
        # smuggle a wider root or global scope through a prebuilt MemoryQuery.
        results = self._as_principal().search(
            query,
            root=root_id,
            scope=selected_scope,
            **kwargs,
        )
        if self.root_id is None or selected_scope in {Scope.GLOBAL, Scope.ANCESTORS}:
            return results
        return [result for result in results if self._memory.contains(self.root_id, result.block.id)]

    def scope(
        self,
        *,
        root: str | MemoryBlock | None = None,
        **kwargs: Any,
    ) -> "MemoryScope":
        """Create a narrower sub-scope.

        Args:
            root: Optional new root. It must be inside this scope.
            **kwargs: Scope options forwarded to `Memory.scope`.

        Returns:
            A new `MemoryScope` with equal or smaller authority.

        Raises:
            AccessDenied: If the requested sub-scope would widen authority.
        """

        requested_principal = kwargs.get("principal")
        if requested_principal is not None and Principal.coerce(requested_principal) != self.principal:
            raise AccessDenied("A sub-scope cannot change principal.")
        if kwargs.get("allow_up") and not self.allow_up:
            raise AccessDenied("A sub-scope cannot add ancestor access.")
        if kwargs.get("allow_global") and not self.allow_global:
            raise AccessDenied("A sub-scope cannot add global access.")
        root_id = _block_id(root) if root is not None else self.root_id
        if root_id is not None:
            self._require_in_subtree(root_id)
        return self._as_principal().scope(root=root_id, **kwargs)

    def _as_principal(self) -> "Memory":
        """Create a full memory handle that shares state but uses this principal."""

        return self._memory.__class__(
            backend=self._memory.backend,
            principal=self.principal,
            policy_engine=self._memory.policy_engine,
            hooks=self._memory.hooks,
            codecs=self._memory.codecs,
        )

    def _require_in_subtree(self, block_id: str | None) -> None:
        """Raise when a block id is outside the scope root."""

        if self.root_id is None or block_id is None:
            return
        if not self._memory.contains(self.root_id, block_id):
            raise AccessDenied(
                f"Memory block {block_id!r} is outside memory scope root {self.root_id!r}."
            )


@overload
def _block_id(block: str | MemoryBlock) -> str: ...


@overload
def _block_id(block: None) -> None: ...


def _block_id(block: str | MemoryBlock | None) -> str | None:
    """Return a block id from a block object, id string, or `None`."""

    if isinstance(block, MemoryBlock):
        return block.id
    return block
