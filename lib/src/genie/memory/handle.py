"""Restricted memory handle for agent access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NoReturn
from uuid import uuid4

from genie.hooks import HandleCapability, HookOperation, HookRegistry, freeze_attributes
from genie.memory._validation import require_non_empty_str
from genie.memory.block import MemoryBlock
from genie.memory.errors import HookRecursionError
from genie.memory.links import LinkRelations, MemoryLink
from genie.policy.engine import AccessDenied, Operation
from genie.policy.policy import MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

if TYPE_CHECKING:
    from genie.hooks import HookContext
    from genie.memory.memory import Memory


class MemoryHandle:
    """Restricted handle to part of a `Memory` object.

    - **Limits:** a handle limits memory access by root, principal, and the
      search or traversal modes that it allows.
    - **Operations:** it supports scoped CRUD, traversal, links, mounts, search,
      and child handles.
    - **Child handles:** a child handle can reduce access more, but it cannot
      increase it.
    - **Audit identity:** each handle has its own `id` and reports its boundary
      to hooks through `attributes`. So an audit trail can tell work through
      one sandbox apart from work by the same principal through another.

    Attributes:
        id: Stable id of this handle. Each handle object has a different id.
        root_id: Block id that is the boundary of the handle, or `None` when
            the handle is unbounded.
        principal: Principal that all operations through this handle act as.
        default_scope: Search scope when the caller gives no `scope` and no
            `MemoryQuery`.
        allow_up: Whether ancestor traversal and search are allowed.
        allow_global: Whether global search is allowed.
        allow_mounts: Whether symbolic mount operations are allowed.
        allow_policy_override: Whether `put` accepts an explicit block policy.
        allow_custom_ids: Whether `put` accepts a caller-provided block id.
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
        allow_custom_ids: bool = False,
    ) -> None:
        """Create a restricted memory handle.

        Args:
            memory: Underlying `Memory` object.
            root_id: Optional handle root block id. `None` means an explicit
                unbounded handle.
            principal: Principal for operations through this handle.
            default_scope: Search scope when the caller gives no search
                `scope` argument.
            allow_up: Whether ancestor traversal and search are allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether this handle can create, list, discover,
                resolve, and update symbolic mounts from local source blocks.
                A mount target can be outside the root. So the handle can
                read each block that its principal can read, if it knows the
                block id and can write a block inside its root.
            allow_policy_override: Whether this handle can give an explicit
                block policy to `put`. When false, new blocks use the default
                policy of the memory.
            allow_custom_ids: Whether this handle can give a caller-provided
                block `id` to `put`. When false, new blocks get generated ids.

        Raises:
            ValueError: If `default_scope` is not a known scope name, or it
                requires a capability that this handle does not enable.
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
        self.allow_custom_ids = allow_custom_ids
        self._boundary_attributes = freeze_attributes(
            _boundary_attributes(
                handle_id=self.id,
                root_id=root_id,
                allow_up=allow_up,
                allow_global=allow_global,
                allow_mounts=allow_mounts,
                allow_policy_override=allow_policy_override,
                allow_custom_ids=allow_custom_ids,
            )
        )

    @property
    def attributes(self) -> Mapping[str, str]:
        """Return the hook context attributes that this handle reports.

        - **Memory attributes:** the mapping starts with the attributes of the
          underlying memory.
        - **Boundary attributes:** it adds `handle.id`, `handle.capabilities`,
          and `handle.root` for a rooted handle. `handle.capabilities` is a
          comma-separated list of the enabled `ancestors`, `global_search`,
          `mounts`, `policy_override`, and `custom_ids` capabilities.
        - **Conflicts:** boundary attributes win on conflict.
        - **Unbounded handles:** an unbounded handle reports no root.
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

        Without `parent`, the handle root is the parent. An unbounded handle
        then creates a block with no parent.

        Args:
            type: Application-defined block type.
            value: User-level value to encode and store.
            parent: Optional parent block or id inside the handle boundary.
            metadata: Optional metadata for filters or indexes.
            policy: Optional block-level access policy. A handle rejects an
                explicit policy, unless it was created with
                `allow_policy_override=True`.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id. A handle rejects it,
                unless it was created with `allow_custom_ids=True`.

        Returns:
            Stored block with decoded value.

        Raises:
            AccessDenied: If the parent is outside the handle boundary, the
                call gives a `policy` and the handle does not allow policy
                overrides, the call gives an `id` and the handle does not
                allow custom ids, or a policy denies the write. A rooted
                handle treats an unknown parent id as outside the handle,
                before the underlying lookup. Without `allow_custom_ids`, a
                handle refuses an `id` before any lookup. So a free id and a
                taken id get the same refusal.
            MemoryConflict: If the handle allows custom ids and a block
                with the same `id` already exists. Block ids are unique in
                the whole backend, so the existing block can be outside the
                handle root.
            MemoryNotFound: If an unbounded handle gets a parent id that does
                not exist.
            TypeError: If `type` or `id` is not a string, `parent` is not a
                block or string id, `policy` is not a `MemoryPolicy`, metadata
                is not a strict JSON dictionary, no codec can encode the value,
                or a `before_put` hook sets a policy that is not a
                `MemoryPolicy`.
            ValueError: If `type`, `id`, or the parent id is empty, a JSON
                value or the metadata nests lists and dictionaries more than
                `MAX_NESTING_DEPTH` levels deep, or a `before_put` hook changes
                a field other than the value, metadata, content type, or
                policy.
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
        if id is not None and not self.allow_custom_ids:
            self._deny(
                HookOperation.PUT,
                HandleCapability.CUSTOM_IDS,
                parent_id,
                "This memory handle does not allow caller-provided block ids.",
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
        """Get a block inside this handle.

        Args:
            block_id: Block or id to get.

        Returns:
            Decoded block, or `None` when an unbounded handle does not find
            it. A rooted handle rejects ids outside its subtree before it gets
            the block, so it raises `AccessDenied` for an unknown id.

        Raises:
            AccessDenied: If the block is outside the handle boundary, or a
                policy denies read access.
            TypeError: If `block_id` is not a block or string id, or no codec
                can decode the stored value.
            ValueError: If `block_id` is empty.
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
            AccessDenied: If the block is outside the handle boundary, or a
                policy denies write or read access. A rooted handle treats an
                unknown block id as outside the handle, before the underlying
                lookup.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id, or no codec
                can encode the value.
            ValueError: If `block_id` is empty, a JSON value nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_update` hook changes a field other than the value,
                metadata, or content type.
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
            value: Dictionary fields to merge into the current decoded value.
            metadata: Metadata fields to merge.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the block is outside the handle boundary, or a
                policy denies write or read access. A rooted handle treats an
                unknown block id as outside the handle, before the underlying
                lookup.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id, `value` is
                provided and the existing decoded value is not a dictionary,
                or metadata is not a strict JSON dictionary.
            ValueError: If `block_id` is empty, a JSON value or the metadata
                nests lists and dictionaries more than `MAX_NESTING_DEPTH`
                levels deep, or a `before_update` hook changes a field other
                than the value, metadata, or content type.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.UPDATE, actual_id)
        return self._as_principal().update(actual_id, value=value, metadata=metadata)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block and its descendants inside this handle.

        - **Recursive delete:** the underlying `Memory` object does the
          recursive delete.
        - **Checks:** it needs write access to the target block only. It does
          not check the policies of the descendants.

        Args:
            block_id: Block or id to delete.

        Raises:
            AccessDenied: If the block is outside the handle boundary, or a
                policy denies write access to it.
            MemoryConflict: If the checked subtree changes before the commit.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.DELETE, actual_id)
        return self._as_principal().delete(actual_id)

    def children(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        """Return readable direct children for a block inside this handle.

        Args:
            block_id: Optional block or id. Defaults to the handle root.

        Returns:
            Decoded child blocks that the principal can read.

        Raises:
            AccessDenied: If the handle is unbounded and the call gives no
                block, the block is outside the handle boundary, or a policy
                denies downward traversal.
            MemoryNotFound: If an unbounded handle gets a parent id that does
                not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
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
        """Return readable ancestors, if this handle allows upward traversal.

        Args:
            block_id: Optional block or id whose parents to traverse. Defaults
                to the handle root.

        Returns:
            Decoded ancestor blocks that the principal can read.

        Raises:
            AccessDenied: If ancestor traversal is off, the handle is unbounded
                and the call gives no block, the block is outside the handle
                boundary, or a policy denies upward traversal.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
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
            AccessDenied: If an endpoint is outside the handle boundary, or a
                policy denies the operation.
            MemoryNotFound: If an unbounded handle gets an endpoint id that
                does not exist.
            TypeError: If `source` or `target` is not a block or string id,
                `relation` is not a string, or metadata is not a strict JSON
                dictionary.
            ValueError: If an endpoint id or `relation` is empty, `relation`
                is the reserved mount relation, metadata nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_add_link` hook changes link identity or topology.
        """

        source_id = _block_id(source)
        target_id = _block_id(target)
        self._require_in_subtree(HookOperation.ADD_LINK, source_id)
        self._require_in_subtree(HookOperation.ADD_LINK, target_id)
        return self._as_principal().add_link(source, target, relation, metadata=metadata)

    def update_link(
        self,
        link: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge metadata into an existing link visible through this handle.

        - **Normal links:** both endpoints are inside the handle boundary.
          Otherwise the call raises `AccessDenied`.
        - **Mounts:** a mount can point outside the boundary, but its update
          needs mount access.

        Args:
            link: Existing link from `add_link` or `list_links`.
            metadata: Metadata fields to merge into the stored link metadata.

        Returns:
            Stored replacement link.

        Raises:
            AccessDenied: If the link is outside the handle boundary, the link
                is a mount and mount access is off, or a policy denies the
                operation. A rooted handle checks the source id of `link`
                before the link lookup. So a missing link and a stored link
                with a source outside the root get the same refusal.
            MemoryNotFound: If the link or a required endpoint is missing.
            TypeError: If `link` is not a `MemoryLink`, or metadata is not a
                strict JSON dictionary.
            ValueError: If metadata nests lists and dictionaries more than
                `MAX_NESTING_DEPTH` levels deep, or a `before_update_link` hook
                changes link identity or topology.
        """

        if not isinstance(link, MemoryLink):
            raise TypeError("link must be a MemoryLink.")
        # The link lookup lists the links of `link.source_id`. So the stored
        # link has the same source, and the boundary check can come first.
        self._require_in_subtree(HookOperation.UPDATE_LINK, link.source_id)
        stored = self._memory._require_stored_link(link)
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
        """Create an acyclic typed link inside this handle, in one atomic step.

        Args:
            source: Source block or id inside the handle boundary.
            target: Target block or id inside the handle boundary.
            relation: Application-defined relation that identifies the DAG.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If an endpoint is outside the handle boundary, or a
                policy denies the operation.
            MemoryConflict: If the link makes a cycle.
            MemoryNotFound: If an unbounded handle gets an endpoint id that
                does not exist.
            TypeError: If `source` or `target` is not a block or string id,
                `relation` is not a string, or metadata is not a strict JSON
                dictionary.
            UnsupportedOperation: If the backend cannot insert DAG edges
                atomically.
            ValueError: If an endpoint id or `relation` is empty, `relation`
                is the reserved mount relation, metadata nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_add_link` hook changes link identity or topology.
        """

        source_id = _block_id(source)
        target_id = _block_id(target)
        self._require_in_subtree(HookOperation.ADD_ACYCLIC_LINK, source_id)
        self._require_in_subtree(HookOperation.ADD_ACYCLIC_LINK, target_id)
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

        - **Boundary filter:** a rooted handle hides links whose other
          endpoint is outside the handle boundary. This also hides mounts to
          targets outside the boundary. `list_mounts` lists those.
        - **Policy filter:** the underlying `Memory` also returns links only
          when the principal can read both endpoints.

        Args:
            block_id: Block or id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Links that match and that this handle can see.

        Raises:
            AccessDenied: If the block is outside the handle boundary, or a
                policy denies read access.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id, or
                `relation` is not a string or `None`.
            ValueError: If `block_id` or `relation` is empty, or `direction`
                is not valid.
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

        - **Outside targets:** the mount target can be outside the handle
          boundary, if the principal can read it. So the handle can mount, and
          then resolve, each block that its principal can read.
        - **Hidden targets:** a rooted handle refuses a missing target and an
          unreadable target outside the boundary in the same way. So the call
          does not show if that block exists.
        - **No subtree change:** the target does not become part of the subtree
          of this handle. A caller gets the target only through an explicit
          `resolve_mount`.

        Args:
            source: Source block or id inside the handle boundary.
            target: Mounted target block or id.
            metadata: Optional mount metadata.

        Returns:
            Stored mount link.

        Raises:
            AccessDenied: If mount access is off, the source is outside the
                handle boundary, the target is outside the boundary and the
                principal cannot read it, or a policy denies `write` on the
                source or `read` on the target. A rooted handle treats an
                unknown source or target id as outside the handle.
            MemoryNotFound: If an unbounded handle gets an endpoint id that
                does not exist.
            TypeError: If `source` or `target` is not a block or string id, or
                metadata is not a strict JSON dictionary.
            ValueError: If an endpoint id is empty, metadata nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_add_link` hook changes link identity or topology.
        """

        self._require_mounts_allowed(HookOperation.ADD_MOUNT)
        self._require_in_subtree(HookOperation.ADD_MOUNT, _block_id(source))
        self._require_readable_mount_target(HookOperation.ADD_MOUNT, _block_id(target))
        return self._as_principal().add_mount(source, target, metadata=metadata)

    def list_mounts(self, source: str | MemoryBlock) -> list[MemoryLink]:
        """List symbolic mounts whose source is inside this handle.

        Args:
            source: Source block or id inside the handle boundary.

        Returns:
            Readable outgoing mount links. A mount whose target route is not
            registered yet comes back as a pending symbolic reference.
            `resolve_mount` returns `None` for it until the target is
            available.

        Raises:
            AccessDenied: If mount access is off, the source is outside the
                handle boundary, or a policy denies read access.
            MemoryNotFound: If an unbounded handle gets a source id that does
                not exist.
            TypeError: If `source` is not a block or string id.
            ValueError: If `source` is empty.
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

        - **Ownership only:** the scan covers only the regular parent and child
          subtree.
        - **No resolve:** it returns mount links whose source is inside the
          selected subtree. It does not resolve those mounts, and it does not
          scan the mounted target trees.

        Args:
            root: Optional scan root inside this handle. Defaults to the handle
                root.
            include_self: If `True`, the result includes the mounts on the
                selected root.

        Returns:
            Readable mount links under the selected root.

        Raises:
            AccessDenied: If mount access is off, the scan root is outside the
                handle boundary, an unbounded handle cannot infer a default
                root, or a policy denies search or downward traversal.
            MemoryNotFound: If an unbounded handle gets a scan root that does
                not exist.
            TypeError: If `root` is not a block or string id.
            ValueError: If `root` is empty.
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

        - **Explicit:** a mount resolution is always an explicit call. It can
          return a target outside the subtree of the handle.
        - **No wider access:** it does not widen ordinary subtree search,
          traversal, or delete behavior.

        Args:
            mount: Mount link from `add_mount` or `list_mounts`.

        Returns:
            Decoded target block, or `None` if the mount or the target no
            longer exists.

        Raises:
            AccessDenied: If mount access is off, the mount source is outside
                the handle boundary, or a policy denies read access on the
                source or the target. A rooted handle also raises it when the
                source block no longer exists.
            MemoryNotFound: If an unbounded handle resolves a mount whose
                source block no longer exists.
            TypeError: If `mount` is not a `MemoryLink`.
            ValueError: If `mount` is not a mount link.
        """

        if not isinstance(mount, MemoryLink):
            raise TypeError("mount must be a MemoryLink.")
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

        - **Scope:** `scope` wins. Without it, a `MemoryQuery` keeps its own
          scope, and other calls use `default_scope`.
        - **Root:** `root` wins. Without it, the root of a `MemoryQuery`, then
          the handle root. A rooted handle accepts only a root inside the
          handle, for each scope. This includes `global`.
        - **Result filter:** for a rooted handle, the results of a `self`,
          `subtree`, or `links` search stay inside the handle root. So a
          `links` search drops linked blocks outside the root. The handle root
          does not filter `global` and `ancestors` results.
        - **`limit` counts results that the handle returns.** A dropped result
          does not use part of the limit.

        Args:
            query: Optional `MemoryQuery` or text query string.
            text: Text query when `query` is not a string.
            scope: Optional search scope. It overrides the default scope.
            root: Optional root inside this handle.
            types: Optional block types to include.
            filters: Optional metadata or block-field filters.
            limit: Maximum number of visible results.

        Returns:
            Search results that the handle can see.

        Raises:
            AccessDenied: If the requested root or search mode goes outside
                the access of the handle, a `before_search` hook changes the
                query to do so, or an unbounded handle cannot infer a
                non-global search root. A rooted handle treats an unknown
                root id as outside the handle.
            MemoryNotFound: If an unbounded handle gets a query root that does
                not exist.
            TypeError: If `root` is not a block or string id, the text, types,
                filters, or limit arguments have the wrong type, or no
                registered codec can decode a visible result.
            ValueError: If the scope is not valid, the root id is empty, the
                limit is negative, a filter uses an unknown operator, or a
                `before_search` hook returns a non-global query with no root.
        """

        if isinstance(query, MemoryQuery):
            selected_scope = Scope(scope or query.scope)
            root_id = _block_id(root) if root is not None else query.root_id or self.root_id
            query = replace(query, root_id=root_id, scope=selected_scope)
        else:
            selected_scope = Scope(scope or self.default_scope)
            root_id = _block_id(root) if root is not None else self.root_id
        refusal = self._search_refusal(selected_scope, root_id)
        if refusal is not None:
            self._deny(HookOperation.SEARCH, *refusal)
        if root_id is None and selected_scope != Scope.GLOBAL:
            self._deny(
                HookOperation.SEARCH,
                HandleCapability.ROOT_REQUIRED,
                None,
                "An unbounded memory handle cannot infer which subtree to search.",
            )
        # The code above put the selected scope and root into the query.
        # - So a caller cannot give a wider root or a global scope through a
        #   prebuilt `MemoryQuery`.
        # - `before_search` hooks can still replace the query. So the handle
        #   checks the query that they return again.
        # - The result filter follows the scope that actually ran.
        return self._as_principal()._search(
            query,
            text=text,
            root=root_id,
            scope=selected_scope,
            types=types,
            filters=filters,
            limit=limit,
            boundary=self._require_hooked_search_inside,
            keep=self._search_result_inside,
        )

    def require_access(self, operation: Operation | str, block_id: str | MemoryBlock) -> None:
        """Raise `AccessDenied` if this handle cannot perform an operation on a block.

        - **No change:** the block does not change, and the method returns
          nothing.
        - **Order:** the handle checks its boundary first, then the block
          policy, as for all other calls.

        Args:
            operation: Built-in `Operation`, or the name of a custom operation
                configured on the policy engine.
            block_id: Block or id that has the policy.

        Raises:
            AccessDenied: If the block is outside the handle boundary, or the
                policy denies the operation. A rooted handle treats an unknown
                block id as outside the handle, before the underlying lookup.
            MemoryNotFound: If an unbounded handle gets a block id that does
                not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty, or `operation` is neither
                built-in nor configured as a custom operation.
        """

        actual_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.REQUIRE_ACCESS, actual_id)
        self._as_principal().require_access(operation, actual_id)

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
        allow_custom_ids: bool = False,
    ) -> MemoryHandle:
        """Create a narrower child handle.

        Args:
            root: Optional new root inside this handle. A root outside this
                handle raises `AccessDenied`.
            principal: Principal for operations through the child handle. When
                given, it is equal to the principal of this handle, with the
                same id, roles, and metadata. Otherwise the call raises
                `AccessDenied`. So a bare id string fails when this principal
                has roles or metadata.
            default_scope: Default search scope of the returned handle.
            allow_up: Whether ancestor traversal and search are allowed. A
                child handle can enable it only if this handle already allows
                it.
            allow_global: Whether global search is allowed. A child handle can
                enable it only if this handle already allows it.
            allow_mounts: Whether symbolic mount operations are allowed. A
                child handle can enable them only if this handle already allows
                them.
            allow_policy_override: Whether the child handle can give explicit
                block policies to `put`. A child handle can enable it only if
                this handle already allows it.
            allow_custom_ids: Whether the child handle can give a
                caller-provided block `id` to `put`. A child handle can enable
                it only if this handle already allows it.

        Returns:
            A new `MemoryHandle` with the same or less access.

        Raises:
            AccessDenied: If the requested child handle increases access, its
                root is outside this handle, or its principal cannot read the
                root.
            MemoryNotFound: If an unbounded handle gets a `root` that does not
                exist.
            TypeError: If `root` is not a block or string id, or `principal`
                is not a principal, id string, or `None`.
            ValueError: If `root` is empty, the scope name is unknown, or
                `default_scope` requires a capability the child handle does
                not enable.
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
        if allow_custom_ids and not self.allow_custom_ids:
            self._deny(
                HookOperation.CREATE_HANDLE,
                HandleCapability.CUSTOM_IDS,
                self.root_id,
                "A child memory handle cannot add custom id access.",
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
                allow_custom_ids=allow_custom_ids,
            )
        return self._as_principal().get_handle(
            root=root_id,
            principal=principal,
            default_scope=default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
            allow_policy_override=allow_policy_override,
            allow_custom_ids=allow_custom_ids,
        )

    def contains(self, root: str | MemoryBlock, block_id: str | MemoryBlock) -> bool:
        """Return whether a readable block is inside a traversable subtree of this handle.

        - **Boundary:** `root` is inside the handle boundary. So each block
          below `root` is also inside it, and the handle does not check the
          candidate against the boundary.
        - **Policy:** the underlying `Memory` object checks `traverse_down` on
          `root` and `read` on the candidate.

        Args:
            root: Potential subtree root block or id inside the handle
                boundary.
            block_id: Candidate block or id.

        Returns:
            `True` when the candidate is the root or is below it, and its read
            policy allows the principal of the handle. A candidate that does
            not exist, that the principal cannot read, or that is outside
            `root` gives `False`.

        Raises:
            AccessDenied: If `root` is outside the handle boundary, or a policy
                denies downward traversal from it. A rooted handle treats an
                unknown root id as outside the handle, before the underlying
                lookup.
            MemoryNotFound: If an unbounded handle gets a root id that does not
                exist.
            TypeError: If `root` or `block_id` is not a block or string id.
            ValueError: If `root` or `block_id` is empty.
        """

        root_id = _block_id(root)
        candidate_id = _block_id(block_id)
        self._require_in_subtree(HookOperation.CONTAINS, root_id)
        return self._as_principal().contains(root_id, candidate_id)

    def _as_principal(self) -> Memory:
        """Create a `Memory` object that shares the state of the memory, but uses the principal and the attributes of this handle."""

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
        """Return the hook registry of the memory."""

        return self._memory.hooks

    def _deny(
        self,
        hook_operation: HookOperation,
        capability: HandleCapability,
        subject_id: str | None,
        message: str,
    ) -> NoReturn:
        """Report a handle boundary refusal to hooks and raise it.

        Handle checks run before the underlying `Memory` operation starts. So
        this method opens one operation context for the denial and its terminal
        event.

        Args:
            hook_operation: Operation that the caller tried.
            capability: Handle boundary or capability that refused.
            subject_id: Block that the refusal applies to, if known.
            message: Message that describes the refusal.

        Raises:
            AccessDenied: Always, with `message`. If the registry cannot report
                the refusal, the caller still gets the same `AccessDenied`,
                with a note.
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
        """Raise `AccessDenied` if symbolic mount operations are off for this handle."""

        if not self.allow_mounts:
            self._deny(
                hook_operation,
                HandleCapability.MOUNTS,
                self.root_id,
                "This memory handle does not allow mount access.",
            )

    def _require_readable_mount_target(self, hook_operation: HookOperation, target_id: str) -> None:
        """Raise `AccessDenied` if a mount target is outside the handle root and the principal cannot read it.

        - **Same refusal:** a missing target and an unreadable target get the
          `SUBTREE` refusal of each other id outside the root. So the call does
          not show if the target exists.
        - **Silent policy check:** the check does not report a `read` refusal.
          The handle reports its own refusal. For a readable target,
          `Memory.add_mount` checks and reports the policies again.
        - **No check:** an unbounded handle, or a target inside the root.
        """

        if self.root_id is None or self._memory._contains_raw(self.root_id, target_id):
            return
        target = self._memory.backend.get_block(target_id)
        if target is None or not self._memory.policy_engine.allows(Operation.READ, target, self.principal):
            self._deny(
                hook_operation,
                HandleCapability.SUBTREE,
                target_id,
                f"Memory block {target_id!r} is outside memory handle root {self.root_id!r}.",
            )

    def _search_refusal(
        self,
        scope: Scope,
        root_id: str | None,
    ) -> tuple[HandleCapability, str | None, str] | None:
        """Return why a search leaves this handle, or `None` if it stays inside.

        This method does not check whether the search has a root:

        - **Before the search:** `search` refuses it as `ROOT_REQUIRED`.
        - **After `before_search` hooks:** `Memory` refuses a non-global query
          with no root with `ValueError`.

        Args:
            scope: Search scope.
            root_id: Search root, or `None` when the search has none.

        Returns:
            The capability that refuses, the block that the refusal applies
            to, and the refusal message, or `None` when the search is allowed.
        """

        if scope == Scope.GLOBAL and not self.allow_global:
            return (
                HandleCapability.GLOBAL_SEARCH,
                self.root_id,
                "This memory handle does not allow global search.",
            )
        if scope == Scope.ANCESTORS and not self.allow_up:
            return (
                HandleCapability.ANCESTORS,
                self.root_id,
                "This memory handle does not allow ancestor search.",
            )
        # `Memory` looks up the root for each scope, also `global`. So the
        # boundary check runs first for each scope, and an unknown root gets
        # the same refusal as a root outside the handle.
        if self.root_id is not None and root_id is not None and not self._memory._contains_raw(self.root_id, root_id):
            return (
                HandleCapability.SUBTREE,
                root_id,
                f"Memory block {root_id!r} is outside memory handle root {self.root_id!r}.",
            )
        return None

    def _require_hooked_search_inside(self, context: HookContext, query: MemoryQuery) -> None:
        """Refuse a query from `before_search` hooks that leaves this handle.

        This check runs inside the search operation. So it reports the refusal
        to `on_denied` in the context of that operation, not in a new one.

        Args:
            context: Active search operation context.
            query: Query returned by the `before_search` hooks.

        Raises:
            AccessDenied: If the query uses a scope that this handle does not
                allow, or a root outside this handle.
        """

        refusal = self._search_refusal(query.scope, query.root_id)
        if refusal is None:
            return
        capability, subject_id, message = refusal
        denied = AccessDenied(message)
        self._hooks.on_denied(context, capability, subject_id, message, error=denied)
        raise denied

    def _search_result_inside(self, query: MemoryQuery, block_id: str) -> bool:
        """Return whether a search result stays inside this handle.

        - **Filtered scopes:** the handle keeps only the `self`, `subtree`,
          and `links` results inside the handle root.
        - **Not filtered:** an unbounded handle, and `global` and `ancestors`
          results. The capabilities of the handle control those scopes.

        Args:
            query: Query after `before_search` hooks.
            block_id: Block id of the result.

        Returns:
            `True` if the handle can return the result.
        """

        if self.root_id is None or query.scope in {Scope.GLOBAL, Scope.ANCESTORS}:
            return True
        return self._memory._contains_raw(self.root_id, block_id)

    def _require_in_subtree(self, hook_operation: HookOperation, block_id: str | None) -> None:
        """Raise `AccessDenied` if a block id is outside the handle root. An unbounded handle or a `None` id passes."""

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
    allow_custom_ids: bool,
) -> dict[str, str]:
    """Describe one handle boundary as hook context attributes.

    - **One stable key:** capabilities are in one key. So a trail can group
      and compare handles with no knowledge of which capabilities exist.
    - **No capabilities:** a handle with no capabilities reports an empty
      string. The key is always present.

    Args:
        handle_id: Stable id of the handle.
        root_id: Handle root, or `None` for an unbounded handle.
        allow_up: Whether ancestor access is enabled.
        allow_global: Whether global search is enabled.
        allow_mounts: Whether mount access is enabled.
        allow_policy_override: Whether explicit block policies are allowed.
        allow_custom_ids: Whether caller-provided block ids are allowed.

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
            ("custom_ids", allow_custom_ids),
        )
        if enabled
    )
    attributes = {"handle.id": handle_id, "handle.capabilities": capabilities}
    if root_id is not None:
        attributes["handle.root"] = root_id
    return attributes


def _block_id(block: str | MemoryBlock) -> str:
    """Return a validated block id from a block object or id string.

    Raises:
        TypeError: If `block` is neither a `MemoryBlock` nor a string.
        ValueError: If `block` is an empty string.
    """

    if isinstance(block, MemoryBlock):
        return block.id
    if not isinstance(block, str):
        raise TypeError("A block reference must be a MemoryBlock or a string id.")
    return require_non_empty_str(block, field_name="Block id")


def _validate_default_scope(
    default_scope: Scope,
    *,
    allow_up: bool,
    allow_global: bool,
) -> None:
    """Raise `ValueError` if a handle default scope requires a capability that is off."""

    if default_scope == Scope.GLOBAL and not allow_global:
        raise ValueError("default_scope='global' requires allow_global=True.")
    if default_scope == Scope.ANCESTORS and not allow_up:
        raise ValueError("default_scope='ancestors' requires allow_up=True.")
