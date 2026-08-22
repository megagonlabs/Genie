"""Restricted memory handle for agent access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NoReturn, overload
from uuid import uuid4

from genie.hooks import HandleCapability, HookOperation, HookRegistry, freeze_attributes
from genie.memory.block import MemoryBlock
from genie.memory.errors import HookRecursionError
from genie.memory.links import LinkRelations, MemoryLink
from genie.policy.engine import AccessDenied
from genie.policy.policy import MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

if TYPE_CHECKING:
    from genie.memory.memory import Memory


class MemoryHandle:
    """Restricted handle to part of a `Memory` object.

    A handle narrows memory access by root, principal, and allowed search or
    traversal modes. It supports scoped CRUD, traversal, links, mounts, search,
    and child handles. Child handles may reduce access further but cannot
    increase it.

    Each handle has its own `id` and reports its boundary to hooks through
    `attributes`, so work done through one sandbox is distinguishable in an
    audit trail from work done by the same principal through another.

    Attributes:
        id: Stable id of this handle, unique per handle object.
        root_id: Block id bounding the handle, or `None` when unbounded.
        principal: Principal every operation through this handle acts as.
        default_scope: Search scope used when callers omit `scope`.
        allow_up: Whether ancestor traversal and search are allowed.
        allow_global: Whether global search is allowed.
        allow_mounts: Whether symbolic mount operations are allowed.
        allow_policy_override: Whether `put` accepts an explicit block policy.
    """

    def __init__(
        self,
        *,
        memory: Memory,
        root_id: str | None,
        principal: Principal,
        default_scope: Scope = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
        allow_mounts: bool = False,
        allow_policy_override: bool = False,
    ) -> None:
        """Create a restricted memory handle.

        Args:
            memory: Underlying `Memory` object.
            root_id: Optional handle root block id. `None` means an explicit
                unbounded handle.
            principal: Principal for operations through this handle.
            default_scope: Search scope used when callers omit the search
                `scope` argument.
            allow_up: Whether ancestor traversal/search is allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether this handle can create, list, and resolve
                symbolic mounts from local source blocks.
            allow_policy_override: Whether this handle can pass an explicit
                block policy to `put`. When false, new blocks use the memory's
                configured default policy.

        Raises:
            ValueError: If `default_scope` is not a known scope name or it
                requires a capability this handle does not enable.
        """

        default_scope = Scope(default_scope)
        _validate_default_scope(
            default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
        )

        self._memory = memory
        self.id = f"handle_{uuid4().hex}"
        self.root_id = root_id
        self.principal = principal
        self.default_scope = default_scope
        self.allow_up = allow_up
        self.allow_global = allow_global
        self.allow_mounts = allow_mounts
        self.allow_policy_override = allow_policy_override
        self._boundary_attributes = freeze_attributes(
            _boundary_attributes(
                handle_id=self.id,
                root_id=root_id,
                allow_up=allow_up,
                allow_global=allow_global,
                allow_mounts=allow_mounts,
                allow_policy_override=allow_policy_override,
            )
        )

    @property
    def attributes(self) -> Mapping[str, str]:
        """Return the hook context attributes reported by this handle.

        The mapping combines the underlying memory's attributes with this
        handle's boundary: `handle.id`, `handle.capabilities` as a
        comma-separated list of the enabled `ancestors`, `global_search`,
        `mounts`, and `policy_override` capabilities, and `handle.root` for a
        rooted handle. Boundary attributes win on conflict, and an unbounded
        handle reports no root.
        """

        return MappingProxyType({**self._memory.attributes, **self._boundary_attributes})

    def put(
        self,
        *,
        type: str,
        value: Any,
        parent: str | MemoryBlock | None = None,
        metadata: dict[str, Any] | None = None,
        policy: MemoryPolicy | None = None,
        content_type: str | None = None,
        id: str | None = None,
    ) -> MemoryBlock:
        """Create a block inside this handle.

        If `parent` is omitted, the handle root is used as the parent.

        Args:
            type: Application-defined block type.
            value: User-level value to encode and store.
            parent: Optional parent block or id inside the handle boundary.
            metadata: Optional metadata used for filtering or indexing.
            policy: Optional block-level access policy. Handles reject explicit
                policies unless created with `allow_policy_override=True`.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Stored block with decoded value.

        Raises:
            AccessDenied: If the parent is outside the handle boundary or
                policy denies the write. In rooted handles, unknown parent ids
                are treated as outside the handle before the underlying lookup
                is reached.
            MemoryNotFound: If the underlying memory handle reaches a missing
                parent id.
            TypeError: If metadata is not a strict JSON dictionary or no codec
                can encode the value.
            ValueError: If a `before_put` hook changes the block's parent id.
        """

        parent_id = _block_id(parent) if parent is not None else self.root_id
        if parent_id is not None:
            self._require_in_subtree(HookOperation.PUT, parent_id)
        if policy is not None and not self.allow_policy_override:
            self._deny(
                HookOperation.PUT,
                HandleCapability.POLICY_OVERRIDE,
                parent_id,
                "This memory handle does not allow explicit block policies.",
            )
        return self._as_principal().put(
            type=type,
            value=value,
            parent=parent_id,
            metadata=metadata,
            policy=policy,
            content_type=content_type,
            id=id,
        )

    def get(self, block_id: str | MemoryBlock) -> MemoryBlock | None:
        """Fetch a block inside this handle.

        Args:
            block_id: Block or id to fetch.

        Returns:
            Decoded block, or `None` when missing in an unbounded handle.
            Rooted handles reject ids outside their subtree before fetching the
            block.

        Raises:
            AccessDenied: If the block is outside the handle boundary or policy
                denies read access.
            TypeError: If no codec can decode the stored value.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.GET, actual_id)
        return self._as_principal().get(actual_id)

    def set(
        self,
        block_id: str | MemoryBlock,
        value: Any,
        *,
        content_type: str | None = None,
    ) -> MemoryBlock:
        """Replace a block value inside this handle.

        Args:
            block_id: Block or id to update.
            value: New user-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the block is outside the handle boundary or policy
                denies write access. In rooted handles, unknown block ids are
                treated as outside the handle before the underlying lookup is
                reached.
            MemoryNotFound: If the underlying memory handle reaches a missing
                block.
            TypeError: If no codec can encode the value.
            ValueError: If a `before_update` hook changes a field other than
                the value, metadata, or content type.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.SET, actual_id)
        return self._as_principal().set(actual_id, value, content_type=content_type)

    def update(
        self,
        block_id: str | MemoryBlock,
        *,
        value: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryBlock:
        """Merge updates into a dictionary value or metadata inside this handle.

        Args:
            block_id: Block or id to update.
            value: Dict fields to merge into the existing decoded value.
            metadata: Metadata fields to merge.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the block is outside the handle boundary or policy
                denies write access. In rooted handles, unknown block ids are
                treated as outside the handle before the underlying lookup is
                reached.
            MemoryNotFound: If the underlying memory handle reaches a missing
                block.
            TypeError: If `value` is provided and the existing decoded value is
                not a dictionary, or metadata is not a strict JSON dictionary.
            ValueError: If a `before_update` hook changes a field other than
                the value, metadata, or content type.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.UPDATE, actual_id)
        return self._as_principal().update(actual_id, value=value, metadata=metadata)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block and its descendants inside this handle.

        Recursive delete is handled by the underlying `Memory` instance and
        requires write access to every block that would be removed.

        Args:
            block_id: Block or id to delete.

        Raises:
            AccessDenied: If the block is outside the handle boundary or policy
                denies write access for any affected block.
            MemoryNotFound: If the underlying memory handle reaches a missing
                block.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.DELETE, actual_id)
        return self._as_principal().delete(actual_id)

    def children(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        """Return readable direct children for a block inside this handle.

        Args:
            block_id: Optional block or id. Defaults to the handle root.

        Returns:
            Decoded child blocks the principal can read.

        Raises:
            AccessDenied: If the handle is unbounded and no block is provided,
                or if the block is outside the handle boundary or policy denies
                downward traversal.
            MemoryNotFound: If the underlying memory handle reaches a missing
                parent.
        """

        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            self._deny(
                HookOperation.CHILDREN,
                HandleCapability.ROOT_REQUIRED,
                None,
                "An unbounded memory handle cannot infer which children to fetch.",
            )
        self._require_in_subtree(HookOperation.CHILDREN, actual_id)
        return self._as_principal().children(actual_id)

    def ancestors(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        """Return readable ancestors when this handle allows upward traversal.

        Args:
            block_id: Optional block or id whose parents should be traversed.
                Defaults to the handle root.

        Returns:
            Decoded ancestor blocks the principal can read.

        Raises:
            AccessDenied: If ancestor traversal is disabled, the handle is
                unbounded and no block is provided, or the block is outside the
                handle boundary or policy denies upward traversal.
            MemoryNotFound: If the underlying memory handle reaches a missing
                block.
        """

        if not self.allow_up:
            self._deny(
                HookOperation.ANCESTORS,
                HandleCapability.ANCESTORS,
                self.root_id,
                "This memory handle does not allow ancestor traversal.",
            )
        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            self._deny(
                HookOperation.ANCESTORS,
                HandleCapability.ROOT_REQUIRED,
                None,
                "An unbounded memory handle cannot infer which ancestors to fetch.",
            )
        self._require_in_subtree(HookOperation.ANCESTORS, actual_id)
        return self._as_principal().ancestors(actual_id)

    def add_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a typed relationship inside this handle.

        Args:
            source: Source block or id inside the handle boundary.
            target: Target block or id inside the handle boundary.
            relation: Application-defined relation type.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If either endpoint is outside the handle boundary or
                policy denies the operation.
            MemoryNotFound: If the underlying memory handle reaches a missing
                endpoint.
            TypeError: If metadata is not a strict JSON dictionary.
            ValueError: If a `before_add_link` hook changes link identity or
                topology.
        """

        self._require_in_subtree(HookOperation.ADD_LINK, _block_id(source))
        self._require_in_subtree(HookOperation.ADD_LINK, _block_id(target))
        return self._as_principal().add_link(source, target, relation, metadata=metadata)

    def update_link(
        self,
        link: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge metadata into an existing link visible through this handle.

        Normal links must keep both endpoints inside the handle boundary. A
        mount may point outside the boundary only when mount access is enabled.

        Args:
            link: Existing link returned by `add_link` or `list_links`.
            metadata: Metadata fields to merge into the stored link metadata.

        Returns:
            Stored replacement link.

        Raises:
            AccessDenied: If the link falls outside the handle boundary, mount
                access is required, or policy denies the operation.
            MemoryNotFound: If the link or a required endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
            ValueError: If a `before_update_link` hook changes link identity or
                topology.
        """

        stored = self._memory._require_stored_link(link)
        self._require_in_subtree(HookOperation.UPDATE_LINK, stored.source_id)
        if stored.relation == LinkRelations.MOUNT:
            self._require_mounts_allowed(HookOperation.UPDATE_LINK)
        else:
            self._require_in_subtree(HookOperation.UPDATE_LINK, stored.target_id)
        return self._as_principal().update_link(stored, metadata=metadata)

    def add_acyclic_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Atomically create an acyclic typed link inside this handle.

        Args:
            source: Source block or id inside the handle boundary.
            target: Target block or id inside the handle boundary.
            relation: Application-defined relation that identifies the DAG.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If either endpoint is outside the handle boundary or
                policy denies the operation.
            MemoryConflict: If the link would form a cycle.
            MemoryNotFound: If either endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
            UnsupportedOperation: If the backend lacks atomic DAG insertion.
            ValueError: If `relation` is the reserved mount relation, or a
                `before_add_link` hook changes link identity or topology.
        """

        self._require_in_subtree(HookOperation.ADD_ACYCLIC_LINK, _block_id(source))
        self._require_in_subtree(HookOperation.ADD_ACYCLIC_LINK, _block_id(target))
        return self._as_principal().add_acyclic_link(
            source,
            target,
            relation,
            metadata=metadata,
        )

    def list_links(
        self,
        block_id: str | MemoryBlock,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block inside this handle.

        Links whose other endpoint falls outside the handle boundary are
        hidden. The underlying `Memory` handle also returns links only when the
        principal can read both endpoints.

        Args:
            block_id: Block or id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links visible through this handle.

        Raises:
            AccessDenied: If the block is outside the handle boundary or policy
                denies read access.
            MemoryNotFound: If the underlying memory handle reaches a missing
                block.
            ValueError: If `direction` is invalid.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.LIST_LINKS, actual_id)
        links = self._as_principal().list_links(actual_id, relation=relation, direction=direction)
        if self.root_id is None:
            return links
        return [
            link
            for link in links
            if self._memory._contains_raw(self.root_id, link.source_id)
            and self._memory._contains_raw(self.root_id, link.target_id)
        ]

    def add_mount(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a symbolic mount from a source inside this handle.

        The mount target may live outside the handle boundary. The target does
        not become part of this handle's subtree; callers must explicitly
        resolve the mount.

        Args:
            source: Source block or id inside the handle boundary.
            target: Mounted target block or id.
            metadata: Optional mount metadata.

        Returns:
            Stored mount link.

        Raises:
            AccessDenied: If mount access is disabled, the source is outside
                the handle boundary, or policy denies the write/read operation.
            MemoryNotFound: If the underlying memory handle reaches a missing
                endpoint.
            TypeError: If metadata is not a strict JSON dictionary.
            ValueError: If a `before_add_link` hook changes link identity or
                topology.
        """

        self._require_mounts_allowed(HookOperation.ADD_MOUNT)
        self._require_in_subtree(HookOperation.ADD_MOUNT, _block_id(source))
        return self._as_principal().add_mount(source, target, metadata=metadata)

    def list_mounts(self, source: str | MemoryBlock) -> list[MemoryLink]:
        """List symbolic mounts whose source is inside this handle.

        Args:
            source: Source block or id inside the handle boundary.

        Returns:
            Readable outgoing mount links. Mounts whose target route has not
            been registered yet are returned as pending symbolic references;
            resolving them returns `None` until the target becomes available.

        Raises:
            AccessDenied: If mount access is disabled, the source is outside
                the handle boundary, or policy denies read access.
            MemoryNotFound: If the underlying memory handle reaches a missing
                source block.
        """

        self._require_mounts_allowed(HookOperation.LIST_MOUNTS)
        actual_id = _block_id(source)
        self._require_in_subtree(HookOperation.LIST_MOUNTS, actual_id)
        return self._as_principal().list_mounts(actual_id)

    def discover_mounts(
        self,
        root: str | MemoryBlock | None = None,
        *,
        include_self: bool = True,
    ) -> list[MemoryLink]:
        """Discover symbolic mounts attached to blocks inside this handle.

        Discovery scans only the regular parent/child subtree. It returns mount
        links whose source is inside the selected subtree, but it does not
        resolve those mounts or include mounted target trees in the scan.

        Args:
            root: Optional scan root inside this handle. Defaults to the handle
                root.
            include_self: Whether mounts on the selected root should be
                included.

        Returns:
            Readable mount links discovered under the selected root.

        Raises:
            AccessDenied: If mount access is disabled, the scan root is outside
                the handle boundary, an unbounded handle cannot infer a default
                root, or policy denies search or downward traversal.
            MemoryNotFound: If the underlying memory handle reaches a missing
                scan root.
        """

        self._require_mounts_allowed(HookOperation.DISCOVER_MOUNTS)
        actual_id = _block_id(root) if root is not None else self.root_id
        if actual_id is None:
            self._deny(
                HookOperation.DISCOVER_MOUNTS,
                HandleCapability.ROOT_REQUIRED,
                None,
                "An unbounded memory handle cannot infer which subtree to scan.",
            )
        self._require_in_subtree(HookOperation.DISCOVER_MOUNTS, actual_id)
        return self._as_principal().discover_mounts(actual_id, include_self=include_self)

    def resolve_mount(self, mount: MemoryLink) -> MemoryBlock | None:
        """Resolve a symbolic mount whose source is inside this handle.

        Resolving a mount is explicit and can return a target outside the
        handle's subtree. It does not widen ordinary subtree search, traversal,
        or delete behavior.

        Args:
            mount: Mount link returned by `add_mount` or `list_mounts`.

        Returns:
            Decoded target block, or `None` if the mount or target no longer
            exists.

        Raises:
            AccessDenied: If mount access is disabled, the mount source is
                outside the handle boundary, or policy denies source/target
                read access.
            MemoryNotFound: If the source block no longer exists.
            ValueError: If `mount` is not a mount link.
        """

        self._require_mounts_allowed(HookOperation.RESOLVE_MOUNT)
        self._require_in_subtree(HookOperation.RESOLVE_MOUNT, mount.source_id)
        return self._as_principal().resolve_mount(mount)

    def search(
        self,
        query: MemoryQuery | str | None = None,
        *,
        text: str | None = None,
        scope: Scope | str | None = None,
        root: str | MemoryBlock | None = None,
        types: list[str] | tuple[str, ...] = (),
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search through this handle.

        Args:
            query: Optional `MemoryQuery` or text query string.
            text: Text query when `query` is not a string.
            scope: Optional search scope overriding the scope default.
            root: Optional root inside this handle.
            types: Optional block types to include.
            filters: Optional metadata or block-field filters.
            limit: Maximum result count.

        Returns:
            Search results visible through the handle.

        Raises:
            AccessDenied: If the requested root or search mode would escape the
                handle's access, or if an unbounded handle cannot infer a
                non-global search root.
            MemoryNotFound: If the query root does not exist.
            ValueError: If the scope is invalid or limit is negative.
        """

        if isinstance(query, MemoryQuery):
            selected_scope = Scope(scope or query.scope)
            root_id = _block_id(root) if root is not None else query.root_id or self.root_id
            query = replace(query, root_id=root_id, scope=selected_scope)
        else:
            selected_scope = Scope(scope or self.default_scope)
            root_id = _block_id(root) if root is not None else self.root_id
        if selected_scope == Scope.GLOBAL and not self.allow_global:
            self._deny(
                HookOperation.SEARCH,
                HandleCapability.GLOBAL_SEARCH,
                self.root_id,
                "This memory handle does not allow global search.",
            )
        if selected_scope == Scope.ANCESTORS and not self.allow_up:
            self._deny(
                HookOperation.SEARCH,
                HandleCapability.ANCESTORS,
                self.root_id,
                "This memory handle does not allow ancestor search.",
            )
        if root_id is None and selected_scope != Scope.GLOBAL:
            self._deny(
                HookOperation.SEARCH,
                HandleCapability.ROOT_REQUIRED,
                None,
                "An unbounded memory handle cannot infer which subtree to search.",
            )
        if root_id is not None and selected_scope != Scope.GLOBAL:
            self._require_in_subtree(HookOperation.SEARCH, root_id)
        # Rebuild query objects with the selected scope/root so a caller cannot
        # pass a wider root or global scope through a prebuilt MemoryQuery.
        results = self._as_principal().search(
            query,
            text=text,
            root=root_id,
            scope=selected_scope,
            types=types,
            filters=filters,
            limit=limit,
        )
        if self.root_id is None or selected_scope in {Scope.GLOBAL, Scope.ANCESTORS}:
            return results
        return [
            result
            for result in results
            if self._memory._contains_raw(self.root_id, result.block.id)
        ]

    def get_handle(
        self,
        *,
        root: str | MemoryBlock | None = None,
        principal: Principal | str | None = None,
        default_scope: Scope | str = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
        allow_mounts: bool = False,
        allow_policy_override: bool = False,
    ) -> MemoryHandle:
        """Create a narrower child handle.

        Args:
            root: Optional new root. It must be inside this handle.
            principal: Principal for operations through the child handle. It
                must match this handle's principal when provided.
            default_scope: Default search scope used by the returned handle.
            allow_up: Whether ancestor traversal/search is allowed. A
                child handle cannot enable it unless this handle already allows
                it.
            allow_global: Whether global search is allowed. A child handle
                cannot enable it unless this handle already allows it.
            allow_mounts: Whether symbolic mount operations are allowed. A
                child handle cannot enable them unless this handle already allows
                them.
            allow_policy_override: Whether the child handle can pass explicit
                block policies to `put`. A child handle cannot enable it unless
                this handle already allows it.

        Returns:
            A new `MemoryHandle` with the same or less access.

        Raises:
            AccessDenied: If the requested child handle would increase access.
            TypeError: If `principal` is not a principal, id string, or `None`.
            ValueError: If the scope name is unknown or `default_scope`
                requires a capability the child handle does not enable.
        """

        if principal is not None and Principal.coerce(principal) != self.principal:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.PRINCIPAL,
                self.root_id,
                "A child memory handle cannot change principal.",
            )
        if allow_up and not self.allow_up:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.ANCESTORS,
                self.root_id,
                "A child memory handle cannot add ancestor access.",
            )
        if allow_global and not self.allow_global:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.GLOBAL_SEARCH,
                self.root_id,
                "A child memory handle cannot add global access.",
            )
        if allow_mounts and not self.allow_mounts:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.MOUNTS,
                self.root_id,
                "A child memory handle cannot add mount access.",
            )
        if allow_policy_override and not self.allow_policy_override:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.POLICY_OVERRIDE,
                self.root_id,
                "A child memory handle cannot add policy override access.",
            )
        root_id = _block_id(root) if root is not None else self.root_id
        if root_id is not None:
            self._require_in_subtree(HookOperation.CREATE_HANDLE, root_id)
        if root_id is None:
            return self._as_principal().get_unbounded_handle(
                principal=principal,
                default_scope=default_scope,
                allow_up=allow_up,
                allow_global=allow_global,
                allow_mounts=allow_mounts,
                allow_policy_override=allow_policy_override,
            )
        return self._as_principal().get_handle(
            root=root_id,
            principal=principal,
            default_scope=default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
            allow_policy_override=allow_policy_override,
        )

    def _as_principal(self) -> Memory:
        """Create a `Memory` handle that shares state but uses this principal."""

        return self._memory.__class__(
            backend=self._memory.backend,
            principal=self.principal,
            policy_engine=self._memory.policy_engine,
            hooks=self._memory.hooks,
            codecs=self._memory.codecs,
            default_policy=self._memory.default_policy,
            attributes=self.attributes,
        )

    @property
    def _hooks(self) -> HookRegistry:
        """Return the hook registry currently configured on the memory."""

        return self._memory.hooks

    def _deny(
        self,
        hook_operation: HookOperation,
        capability: HandleCapability,
        subject_id: str | None,
        message: str,
    ) -> NoReturn:
        """Report a handle boundary refusal to hooks and raise it.

        Handle checks run before the underlying `Memory` operation starts, so
        this opens one operation context to carry the denial and its terminal
        event.

        Args:
            hook_operation: Operation the caller attempted.
            capability: Handle boundary or capability that refused.
            subject_id: Block the refusal applies to, when one is known.
            message: Message describing the refusal.

        Raises:
            AccessDenied: Always, carrying `message`. A registry that cannot
                report the refusal does not change what the caller receives.
        """

        denied = AccessDenied(message)
        try:
            with self._hooks.operation(
                hook_operation,
                principal_id=self.principal.id,
                attributes=self.attributes,
            ) as context:
                self._hooks.on_denied(context, capability, subject_id, message, error=denied)
                raise denied
        except HookRecursionError as unreportable:
            denied.add_note(f"Refusal could not be reported: {unreportable!r}")
            raise denied

    def _require_mounts_allowed(self, hook_operation: HookOperation) -> None:
        """Raise when symbolic mount operations are disabled for this handle."""

        if not self.allow_mounts:
            self._deny(
                hook_operation,
                HandleCapability.MOUNTS,
                self.root_id,
                "This memory handle does not allow mount access.",
            )

    def _require_in_subtree(self, hook_operation: HookOperation, block_id: str | None) -> None:
        """Raise when a block id is outside the handle root."""

        if self.root_id is None or block_id is None:
            return
        if not self._memory._contains_raw(self.root_id, block_id):
            self._deny(
                hook_operation,
                HandleCapability.SUBTREE,
                block_id,
                f"Memory block {block_id!r} is outside memory handle root {self.root_id!r}.",
            )


def _boundary_attributes(
    *,
    handle_id: str,
    root_id: str | None,
    allow_up: bool,
    allow_global: bool,
    allow_mounts: bool,
    allow_policy_override: bool,
) -> dict[str, str]:
    """Describe one handle boundary as hook context attributes.

    Capabilities are reported as one stable key so a trail can be grouped and
    compared without knowing which capabilities exist. A handle with no
    capabilities reports an empty list rather than omitting the key.

    Args:
        handle_id: Stable id of the handle being described.
        root_id: Handle root, or `None` for an unbounded handle.
        allow_up: Whether ancestor access is enabled.
        allow_global: Whether global search is enabled.
        allow_mounts: Whether mount access is enabled.
        allow_policy_override: Whether explicit block policies are allowed.

    Returns:
        Attributes describing the handle. `handle.root` is omitted for an
        unbounded handle.
    """

    capabilities = ",".join(
        name
        for name, enabled in (
            ("ancestors", allow_up),
            ("global_search", allow_global),
            ("mounts", allow_mounts),
            ("policy_override", allow_policy_override),
        )
        if enabled
    )
    attributes = {"handle.id": handle_id, "handle.capabilities": capabilities}
    if root_id is not None:
        attributes["handle.root"] = root_id
    return attributes


@overload
def _block_id(block: str | MemoryBlock) -> str: ...


@overload
def _block_id(block: None) -> None: ...


def _block_id(block: str | MemoryBlock | None) -> str | None:
    """Return a block id from a block object, id string, or `None`."""

    if isinstance(block, MemoryBlock):
        return block.id
    return block


def _validate_default_scope(
    default_scope: Scope,
    *,
    allow_up: bool,
    allow_global: bool,
) -> None:
    """Raise when a handle default scope requires a disabled capability."""

    if default_scope == Scope.GLOBAL and not allow_global:
        raise ValueError("default_scope='global' requires allow_global=True.")
    if default_scope == Scope.ANCESTORS and not allow_up:
        raise ValueError("default_scope='ancestors' requires allow_up=True.")
