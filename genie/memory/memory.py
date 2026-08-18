"""Main memory system interface."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, overload

from genie.backend.base import MemoryBackend
from genie.backend.in_memory import InMemoryBackend
from genie.backend.structure_capabilities import AcyclicLinkStore
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec
from genie.hooks import HookContext, HookOperation, HookRegistry, MemoryHook
from genie.memory.block import MemoryBlock, new_memory_id, utc_now
from genie.memory.errors import HookRecursionError, MemoryNotFound, UnsupportedOperation
from genie.memory.links import LinkRelations, MemoryLink
from genie.policy.engine import AccessDenied, Operation, PolicyEngine
from genie.policy.policy import MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult
from genie.query.filters import normalize_filters

if TYPE_CHECKING:
    from genie.memory.handle import MemoryHandle


class Memory:
    """Main interface for a Genie memory system.

    `Memory` configures the backend, codecs, hooks, policy engine, default
    principal, and default policy. Trusted application code usually keeps this
    object and passes bounded `MemoryHandle` objects to agents. It coordinates
    parent/child ownership, scoped access, links, mounts, search, hooks, and
    codecs without owning application business logic, memory structure, storage
    layout, retrieval strategy, or value meaning.
    """

    def __init__(
        self,
        *,
        backend: MemoryBackend | None = None,
        principal: Principal | str | None = None,
        policy_engine: PolicyEngine | None = None,
        hooks: list[MemoryHook] | HookRegistry | None = None,
        codecs: CodecRegistry | None = None,
        default_policy: MemoryPolicy | None = None,
    ) -> None:
        """Create the main memory interface.

        Args:
            backend: Backend adapter. Defaults to `InMemoryBackend`.
            principal: Acting principal for operations through this memory.
            policy_engine: Policy evaluator. Defaults to `PolicyEngine`.
            hooks: Hook list or registry.
            codecs: Codec registry. Defaults to a registry containing
                `JsonCodec`.
            default_policy: Policy attached to new blocks when
                `put(..., policy=None)`. Defaults to
                `MemoryPolicy.owner_private()`.

        Raises:
            TypeError: If a configured backend, principal, policy engine,
                codec registry, or default policy has the wrong type.
        """

        if backend is not None and not isinstance(backend, MemoryBackend):
            raise TypeError("backend must implement the MemoryBackend protocol.")
        if policy_engine is not None and not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be a PolicyEngine.")
        if codecs is not None and not isinstance(codecs, CodecRegistry):
            raise TypeError("codecs must be a CodecRegistry.")
        if default_policy is not None and not isinstance(default_policy, MemoryPolicy):
            raise TypeError("default_policy must be a MemoryPolicy.")
        self.backend: MemoryBackend = InMemoryBackend() if backend is None else backend
        self.principal = Principal.coerce(principal)
        self.policy_engine = PolicyEngine() if policy_engine is None else policy_engine
        if isinstance(hooks, HookRegistry):
            self.hooks = hooks
        else:
            self.hooks = HookRegistry(hooks)
        self.codecs = CodecRegistry([JsonCodec()]) if codecs is None else codecs
        self.default_policy = (
            MemoryPolicy.owner_private() if default_policy is None else default_policy
        )

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
        """Create a new memory block.

        Args:
            type: Application-defined block type.
            value: User-level value to encode and store.
            parent: Optional parent block or id.
            metadata: Optional metadata used for filtering or indexing.
            policy: Optional block-level access policy. When omitted, the
                memory's `default_policy` is attached.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Stored block with decoded value.

        Raises:
            AccessDenied: If the principal cannot write under the parent.
            MemoryNotFound: If the parent id does not exist.
            TypeError: If metadata is not a strict JSON dictionary or no codec
                can encode the value.
        """

        with self.hooks.operation(
            HookOperation.PUT,
            principal_id=self.principal.id,
        ) as context:
            if policy is not None and not isinstance(policy, MemoryPolicy):
                raise TypeError("policy must be a MemoryPolicy or None.")
            parent_id = _block_id(parent)
            if parent_id is not None:
                parent_block = self._require_raw(parent_id)
                self._require_access(context, Operation.WRITE, parent_block)
                self._require_access(context, Operation.TRAVERSE_DOWN, parent_block)
            encoded = self.codecs.encode(value, content_type)
            block = MemoryBlock(
                id=id or new_memory_id(),
                type=type,
                value=encoded.data,
                parent_id=parent_id,
                content_type=encoded.content_type,
                metadata=_copy_metadata(metadata),
                policy=policy if policy is not None else self.default_policy,
                created_by=self.principal.id,
            )
            block = self.hooks.before_put(context, block)
            if block.parent_id != parent_id:
                raise ValueError("before_put hooks cannot change a block's parent_id.")
            block = block.with_updates(metadata=_copy_metadata(block.metadata))
            stored = self.backend.put_block(block)
            self.hooks.after_put(context, stored)
            return self._decode_block(stored)

    def get(self, block_id: str | MemoryBlock) -> MemoryBlock | None:
        """Fetch a memory block by id.

        Args:
            block_id: Block or block id.

        Returns:
            Decoded block, or `None` when missing.

        Raises:
            AccessDenied: If the principal cannot read the block.
            TypeError: If no codec can decode the stored value.
        """

        with self.hooks.operation(
            HookOperation.GET,
            principal_id=self.principal.id,
        ) as context:
            actual_id = _block_id(block_id)
            self.hooks.before_get(context, actual_id)
            block = self.backend.get_block(actual_id)
            if block is None:
                self.hooks.after_get(context, None)
                return None
            self._require_access(context, Operation.READ, block)
            decoded = self._decode_block(block)
            self.hooks.after_get(context, decoded)
            return decoded

    def set(
        self,
        block_id: str | MemoryBlock,
        value: Any,
        *,
        content_type: str | None = None,
    ) -> MemoryBlock:
        """Replace a block's value.

        Args:
            block_id: Block or block id to update.
            value: New user-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the principal cannot write the block.
            MemoryConflict: If another writer changes the block before commit.
            MemoryNotFound: If the block does not exist.
            TypeError: If no codec can encode the value.
        """

        with self.hooks.operation(
            HookOperation.SET,
            principal_id=self.principal.id,
        ) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.WRITE, block)
            encoded = self.codecs.encode(value, content_type)
            proposed = block.with_updates(
                value=encoded.data,
                content_type=encoded.content_type,
                metadata=deepcopy(block.metadata),
                updated_at=utc_now(),
                version=block.version + 1,
            )
            updated = self.hooks.before_update(context, block, proposed)
            updated = updated.with_updates(metadata=_copy_metadata(updated.metadata))
            _require_update_hook_invariants(proposed, updated)
            stored = self.backend.update_block(updated, expected_version=block.version)
            self.hooks.after_update(context, block, stored)
            return self._decode_block(stored)

    def update(
        self,
        block_id: str | MemoryBlock,
        *,
        value: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryBlock:
        """Merge updates into a dictionary value or metadata.

        Args:
            block_id: Block or block id to update.
            value: Dict fields to merge into the existing decoded value.
            metadata: Metadata fields to merge.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the principal cannot write the block.
            MemoryConflict: If another writer changes the block before commit.
            MemoryNotFound: If the block does not exist.
            TypeError: If `value` is provided and the existing decoded value is
                not a dictionary, or metadata is not a strict JSON dictionary.
        """

        with self.hooks.operation(
            HookOperation.UPDATE,
            principal_id=self.principal.id,
        ) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.WRITE, block)
            decoded_value = self.codecs.decode(EncodedValue(block.content_type, block.value))
            if value is not None:
                if not isinstance(decoded_value, dict):
                    raise TypeError("Value updates require the existing memory value to be a dict.")
                decoded_value = {**decoded_value, **value}
            encoded = self.codecs.encode(decoded_value, block.content_type)
            updated_metadata = _copy_metadata(
                {**block.metadata, **_copy_metadata(metadata)}
            )
            proposed = block.with_updates(
                value=encoded.data,
                metadata=updated_metadata,
                updated_at=utc_now(),
                version=block.version + 1,
            )
            updated = self.hooks.before_update(context, block, proposed)
            updated = updated.with_updates(metadata=_copy_metadata(updated.metadata))
            _require_update_hook_invariants(proposed, updated)
            stored = self.backend.update_block(updated, expected_version=block.version)
            self.hooks.after_update(context, block, stored)
            return self._decode_block(stored)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block and its descendants.

        Recursive delete requires write access to every block that would be
        removed.

        Args:
            block_id: Block or block id to delete.

        Raises:
            AccessDenied: If any affected block denies write access.
            MemoryConflict: If the checked subtree changes before commit.
            MemoryNotFound: If the block does not exist.
        """

        with self.hooks.operation(
            HookOperation.DELETE,
            principal_id=self.principal.id,
        ) as context:
            block = self._require_raw(_block_id(block_id))
            blocks_to_delete = self.backend.descendants(block.id, include_self=True)
            for candidate in blocks_to_delete:
                self._require_access(context, Operation.WRITE, candidate)
            decoded_blocks = [self._decode_block(candidate) for candidate in blocks_to_delete]
            for decoded_block in decoded_blocks:
                self.hooks.before_delete(context, decoded_block)
            self.backend.delete_block(
                block.id,
                expected_versions={candidate.id: candidate.version for candidate in blocks_to_delete},
            )
            for decoded_block in decoded_blocks:
                self.hooks.after_delete(context, decoded_block)

    def children(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
        """Return readable direct children for a block.

        Args:
            block_id: Parent block or id.

        Returns:
            Decoded child blocks the principal can read.

        Raises:
            AccessDenied: If the principal cannot traverse downward from the
                parent.
            MemoryNotFound: If the parent does not exist.
        """

        block = self._require_raw(_block_id(block_id))
        self._require_access_outside_operation(HookOperation.CHILDREN, Operation.TRAVERSE_DOWN, block)
        return [
            self._decode_block(child)
            for child in self.backend.children(block.id)
            if self.policy_engine.allows(Operation.READ, child, self.principal)
        ]

    def ancestors(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
        """Return readable ancestors for a block.

        Args:
            block_id: Block or id whose parents should be traversed.

        Returns:
            Decoded ancestor blocks the principal can read.

        Raises:
            AccessDenied: If the principal cannot traverse upward from the
                block.
            MemoryNotFound: If the block does not exist.
        """

        block = self._require_raw(_block_id(block_id))
        self._require_access_outside_operation(HookOperation.ANCESTORS, Operation.TRAVERSE_UP, block)
        return [
            self._decode_block(ancestor)
            for ancestor in self.backend.ancestors(block.id)
            if self.policy_engine.allows(Operation.READ, ancestor, self.principal)
        ]

    def add_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a typed non-hierarchical relationship between two blocks.

        Args:
            source: Source block or id.
            target: Target block or id.
            relation: Application-defined relation type.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target.
            MemoryNotFound: If either endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
        """

        return self._add_link(
            source,
            target,
            relation,
            metadata=metadata,
            hook_operation=HookOperation.ADD_LINK,
            acyclic=False,
        )

    def update_link(
        self,
        link: MemoryLink,
        *,
        metadata: dict[str, Any],
    ) -> MemoryLink:
        """Merge metadata into an existing typed link.

        Link identity and topology remain unchanged. The returned object is a
        replacement value representing the state persisted by the backend.

        Args:
            link: Existing link returned by `add_link` or `list_links`.
            metadata: Metadata fields to merge into the stored link metadata.

        Returns:
            Stored replacement link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target. A pending mount without a registered target is governed
                by its source policy.
            MemoryNotFound: If the link or a non-mount endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
        """

        with self.hooks.operation(
            HookOperation.UPDATE_LINK,
            principal_id=self.principal.id,
        ) as context:
            stored = self._require_stored_link(link)
            source = self._require_raw(stored.source_id)
            self._require_access(context, Operation.WRITE, source)
            target = self.backend.get_block(stored.target_id)
            if target is None:
                if stored.relation != LinkRelations.MOUNT:
                    raise MemoryNotFound(
                        f"Target memory block {stored.target_id!r} does not exist."
                    )
            else:
                self._require_access(context, Operation.READ, target)
            updated = replace(
                stored,
                metadata=_copy_metadata(
                    {**stored.metadata, **_copy_metadata(metadata)}
                ),
            )
            updated = self.hooks.before_update_link(context, stored, updated)
            updated = replace(updated, metadata=_copy_metadata(updated.metadata))
            _require_link_hook_invariants(stored, updated, callback="before_update_link")
            persisted = self.backend.update_link(updated)
            self.hooks.after_update_link(context, stored, persisted)
            return persisted

    def add_acyclic_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Atomically create a typed link unless it would form a cycle.

        Cycle detection includes every stored link with the exact same
        relation. The backend performs the reachability check and insertion as
        one operation.

        Args:
            source: Source block or id.
            target: Target block or id.
            relation: Application-defined relation that identifies the DAG.
            metadata: Optional relationship metadata.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target.
            MemoryConflict: If existing or proposed links would contain a cycle.
            MemoryNotFound: If either endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
            UnsupportedOperation: If the backend lacks atomic DAG insertion.
        """

        if relation == LinkRelations.MOUNT:
            raise ValueError("The reserved mount relation cannot be acyclically inserted.")
        return self._add_link(
            source,
            target,
            relation,
            metadata=metadata,
            hook_operation=HookOperation.ADD_ACYCLIC_LINK,
            acyclic=True,
        )

    def _add_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None,
        hook_operation: HookOperation,
        acyclic: bool,
    ) -> MemoryLink:
        """Authorize, hook, and persist one link mutation."""

        with self.hooks.operation(
            hook_operation,
            principal_id=self.principal.id,
        ) as context:
            source_block = self._require_raw(_block_id(source))
            target_block = self._require_raw(_block_id(target))
            self._require_access(context, Operation.WRITE, source_block)
            self._require_access(context, Operation.READ, target_block)
            acyclic_backend: AcyclicLinkStore | None = None
            if acyclic:
                if not isinstance(self.backend, AcyclicLinkStore):
                    raise UnsupportedOperation(
                        "The configured backend does not support atomic acyclic links."
                    )
                acyclic_backend = self.backend
            proposed = MemoryLink(
                source_id=source_block.id,
                target_id=target_block.id,
                relation=relation,
                metadata=_copy_metadata(metadata),
            )
            hooked = self.hooks.before_add_link(context, proposed)
            hooked = replace(hooked, metadata=_copy_metadata(hooked.metadata))
            _require_link_hook_invariants(proposed, hooked, callback="before_add_link")
            if acyclic_backend is not None:
                stored = acyclic_backend.add_acyclic_link(hooked)
            else:
                stored = self.backend.add_link(hooked)
            self.hooks.after_add_link(context, stored)
            return stored

    def list_links(
        self,
        block_id: str | MemoryBlock,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List readable links connected to a block.

        Links are returned only when the principal can read both endpoints.

        Args:
            block_id: Block or id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            AccessDenied: If the principal cannot read `block_id`.
            MemoryNotFound: If `block_id` does not exist.
            ValueError: If `direction` is invalid.
        """

        block = self._require_raw(_block_id(block_id))
        self._require_access_outside_operation(HookOperation.LIST_LINKS, Operation.READ, block)
        return [
            link
            for link in self.backend.list_links(block.id, relation=relation, direction=direction)
            if self._can_read_link_endpoints(link)
        ]

    def add_mount(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a symbolic mount from one block to another block.

        A mount is stored as a typed link with the reserved
        `LinkRelations.MOUNT` relation. It does not make the target a child of
        the source, does not affect subtree traversal, and is not followed by
        recursive delete.

        Args:
            source: Source block or id where the mount link starts.
            target: Mounted target root block or id.
            metadata: Optional mount metadata.

        Returns:
            Stored mount link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target.
            MemoryNotFound: If either endpoint is missing.
            TypeError: If metadata is not a strict JSON dictionary.
        """

        return self._add_link(
            source,
            target,
            LinkRelations.MOUNT,
            metadata=metadata,
            hook_operation=HookOperation.ADD_MOUNT,
            acyclic=False,
        )

    def list_mounts(self, source: str | MemoryBlock) -> list[MemoryLink]:
        """List symbolic mounts that start from a block.

        Args:
            source: Source block or id where the mount link starts.

        Returns:
            Readable outgoing mount links. Mounts whose target route has not
            been registered yet are returned as pending symbolic references;
            resolving them returns `None` until the target becomes available.
        """

        source_block = self._require_raw(_block_id(source))
        self._require_access_outside_operation(HookOperation.LIST_MOUNTS, Operation.READ, source_block)
        return [
            link
            for link in self.backend.list_links(
                source_block.id,
                relation=LinkRelations.MOUNT,
                direction="out",
            )
            if self._can_read_mount(link)
        ]

    def discover_mounts(
        self,
        root: str | MemoryBlock,
        *,
        include_self: bool = True,
    ) -> list[MemoryLink]:
        """Discover symbolic mounts attached to blocks in a regular subtree.

        Discovery scans the normal parent/child subtree below `root` and
        returns readable outgoing mount links found on those source blocks. It
        does not resolve mount targets and does not traverse into mounted
        target trees.

        Args:
            root: Subtree root block or id to scan.
            include_self: Whether mounts on `root` itself should be included.

        Returns:
            Readable mount links whose source is inside the scanned subtree.

        Raises:
            AccessDenied: If the principal cannot search and traverse downward
                from `root`.
            MemoryNotFound: If `root` does not exist.
        """

        root_id = _block_id(root)
        root_block = self._require_raw(root_id)
        self._require_access_outside_operation(HookOperation.DISCOVER_MOUNTS, Operation.SEARCH, root_block)
        self._require_access_outside_operation(HookOperation.DISCOVER_MOUNTS, Operation.TRAVERSE_DOWN, root_block)

        mounts: list[MemoryLink] = []
        for candidate in self.backend.descendants(root_id, include_self=include_self):
            if not self.policy_engine.allows(Operation.READ, candidate, self.principal):
                continue
            mounts.extend(
                link
                for link in self.backend.list_links(
                    candidate.id,
                    relation=LinkRelations.MOUNT,
                    direction="out",
                )
                if self._can_read_mount(link)
            )
        return mounts

    def resolve_mount(self, mount: MemoryLink) -> MemoryBlock | None:
        """Resolve a mount link to its target block.

        Args:
            mount: Mount link returned by `add_mount` or `list_mounts`.

        Returns:
            Decoded target block, or `None` if the mount or target no longer
            exists.

        Raises:
            ValueError: If `mount` is not a mount link.
            AccessDenied: If the principal cannot read the source or target.
            MemoryNotFound: If the source block no longer exists.
        """

        if mount.relation != LinkRelations.MOUNT:
            raise ValueError(f"Expected mount relation {LinkRelations.MOUNT!r}.")
        source = self._require_raw(mount.source_id)
        self._require_access_outside_operation(HookOperation.RESOLVE_MOUNT, Operation.READ, source)
        stored_mount = next(
            (
                candidate
                for candidate in self.backend.list_links(
                    source.id,
                    relation=LinkRelations.MOUNT,
                    direction="out",
                )
                if candidate.id == mount.id and candidate.target_id == mount.target_id
            ),
            None,
        )
        if stored_mount is None:
            return None
        return self.get(stored_mount.target_id)

    def search(
        self,
        query: MemoryQuery | str | None = None,
        *,
        text: str | None = None,
        root: str | MemoryBlock | None = None,
        scope: Scope | str = Scope.GLOBAL,
        types: list[str] | tuple[str, ...] = (),
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Search memory blocks.

        Args:
            query: Optional `MemoryQuery` or text query string.
            text: Text query when `query` is not a string.
            root: Optional root block or id for scoped search.
            scope: Search scope.
            types: Optional block types to include.
            filters: Optional metadata or block-field filters.
            limit: Maximum result count.

        Returns:
            Search results with decoded blocks.

        Raises:
            AccessDenied: If the principal cannot search or traverse from the
                query root.
            MemoryNotFound: If the query root does not exist.
            ValueError: If the scope is invalid, the limit is negative, or a
                non-global scope has no root.
        """

        with self.hooks.operation(
            HookOperation.SEARCH,
            principal_id=self.principal.id,
        ) as context:
            memory_query = self._coerce_query(
                query,
                text=text,
                root=root,
                scope=scope,
                types=types,
                filters=filters,
                limit=limit,
            )
            self._authorize_query(context, memory_query)
            memory_query = self.hooks.before_search(context, memory_query)
            # Hooks may return a freshly constructed query. Normalize once more so
            # backends receive canonical filter operators, then check access again
            # so hooks cannot pass through a wider query root or scope.
            memory_query = replace(memory_query, filters=normalize_filters(memory_query.filters))
            self._authorize_query(context, memory_query)
            raw_results = self.backend.search(memory_query)
            decoded_results = [
                replace(result, block=self._decode_block(result.block))
                for result in raw_results
            ]
            visible_results = self._filter_visible_results(memory_query, decoded_results)
            hooked_results = self.hooks.after_search(
                context,
                memory_query,
                visible_results,
            )
            return self._filter_visible_results(memory_query, hooked_results)

    def get_handle(
        self,
        *,
        root: str | MemoryBlock,
        principal: Principal | str | None = None,
        default_scope: Scope | str = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
        allow_mounts: bool = False,
        allow_policy_override: bool = False,
    ) -> "MemoryHandle":
        """Create a bounded memory handle to pass to agents.

        Args:
            root: Root block or id that bounds the handle.
            principal: Principal for operations through the handle. Defaults to
                this `Memory` handle's principal.
            default_scope: Default search scope used by the returned handle.
            allow_up: Whether ancestor traversal/search is allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether the handle can create, list, and resolve
                symbolic mounts whose source is inside the handle.
            allow_policy_override: Whether the handle can pass explicit block
                policies to `put`. Leave false for untrusted agent code.

        Returns:
            A `MemoryHandle` backed by this memory system.

        Raises:
            AccessDenied: If the handle principal cannot read `root`.
            ValueError: If `root` is `None`.
            MemoryNotFound: If `root` does not exist.
        """

        root_id = _block_id(root)
        if root_id is None:
            raise ValueError(
                "Memory.get_handle() requires a root. Use Memory.get_unbounded_handle() "
                "for explicit unbounded access."
            )
        root_block = self._require_raw(root_id)
        handle_principal = Principal.coerce(principal) if principal is not None else self.principal
        self._require_access_outside_operation(
            HookOperation.CREATE_HANDLE,
            Operation.READ,
            root_block,
            principal=handle_principal,
        )
        return self._new_handle(
            root_id=root_id,
            principal=handle_principal,
            default_scope=default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
            allow_policy_override=allow_policy_override,
        )

    def get_unbounded_handle(
        self,
        *,
        principal: Principal | str | None = None,
        default_scope: Scope | str = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
        allow_mounts: bool = False,
        allow_policy_override: bool = False,
    ) -> "MemoryHandle":
        """Create an explicit unbounded memory handle.

        An unbounded handle has no default subtree root. It can still be limited
        by principal policies and options such as `allow_global`.
        Non-global searches through an unbounded handle must provide an explicit
        root.

        Args:
            principal: Principal for operations through the handle. Defaults to
                this `Memory` handle's principal.
            default_scope: Default search scope used by the returned handle.
            allow_up: Whether ancestor traversal/search is allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether the handle can create, list, and resolve
                symbolic mounts whose source is visible through the handle.
            allow_policy_override: Whether the handle can pass explicit block
                policies to `put`. Leave false for untrusted agent code.

        Returns:
            An unbounded `MemoryHandle` backed by this memory system.
        """

        return self._new_handle(
            root_id=None,
            principal=principal,
            default_scope=default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
            allow_policy_override=allow_policy_override,
        )

    def contains(self, root: str | MemoryBlock, block_id: str | MemoryBlock) -> bool:
        """Return whether a readable block is inside a traversable subtree.

        Args:
            root: Potential subtree root block or id.
            block_id: Candidate block or id.

        Returns:
            `True` when the candidate is equal to or below the root and its
            read policy permits the current principal. Missing candidates and
            unreadable candidates return `False`.

        Raises:
            AccessDenied: If downward traversal from the root is denied.
            MemoryNotFound: If the root does not exist.
        """

        root_id = _block_id(root)
        candidate_id = _block_id(block_id)
        root_block = self._require_raw(root_id)
        self._require_access_outside_operation(HookOperation.CONTAINS, Operation.TRAVERSE_DOWN, root_block)
        candidate = self.backend.get_block(candidate_id)
        if candidate is None or not self._contains_raw(root_id, candidate_id):
            return False
        return self.policy_engine.allows(Operation.READ, candidate, self.principal)

    def _contains_raw(self, root_id: str, block_id: str) -> bool:
        """Return backend ownership membership without policy evaluation."""

        return self.backend.contains(root_id, block_id)

    def _new_handle(
        self,
        *,
        root_id: str | None,
        principal: Principal | str | None,
        default_scope: Scope | str,
        allow_up: bool,
        allow_global: bool,
        allow_mounts: bool,
        allow_policy_override: bool,
    ) -> "MemoryHandle":
        """Construct a `MemoryHandle` after root validation is complete."""

        from genie.memory.handle import MemoryHandle

        return MemoryHandle(
            memory=self,
            root_id=root_id,
            principal=Principal.coerce(principal) if principal is not None else self.principal,
            default_scope=Scope(default_scope),
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
            allow_policy_override=allow_policy_override,
        )

    def _coerce_query(
        self,
        query: MemoryQuery | str | None,
        *,
        text: str | None,
        root: str | MemoryBlock | None,
        scope: Scope | str,
        types: list[str] | tuple[str, ...],
        filters: dict[str, Any] | None,
        limit: int,
    ) -> MemoryQuery:
        """Convert friendly search arguments into a `MemoryQuery`."""

        if isinstance(query, MemoryQuery):
            return query
        if isinstance(query, str):
            text = query
        return MemoryQuery(
            text=text,
            root_id=_block_id(root) if root is not None else None,
            scope=scope,
            types=types,
            filters={} if filters is None else filters,
            limit=limit,
        )

    def _authorize_query(self, context: HookContext, query: MemoryQuery) -> None:
        """Check access required by a query root and scope."""

        if query.root_id is None:
            if query.scope != Scope.GLOBAL:
                raise ValueError(
                    f"Search scope {query.scope.value!r} requires a root block."
                )
            return
        root = self._require_raw(query.root_id)
        self._require_access(context, Operation.SEARCH, root)
        if query.scope == Scope.SUBTREE:
            self._require_access(context, Operation.TRAVERSE_DOWN, root)
        elif query.scope == Scope.ANCESTORS:
            self._require_access(context, Operation.TRAVERSE_UP, root)
        elif query.scope == Scope.LINKS:
            self._require_access(context, Operation.READ, root)

    def _require_access(
        self,
        context: HookContext,
        operation: Operation | str,
        block: MemoryBlock,
        *,
        principal: Principal | None = None,
    ) -> None:
        """Require policy access inside an operation and report refusals.

        Args:
            context: Active operation context.
            operation: Policy operation to check.
            block: Block carrying the policy.
            principal: Principal to check. Defaults to this memory's principal.

        Raises:
            AccessDenied: If the block policy does not allow the operation.
        """

        acting = self.principal if principal is None else principal
        try:
            self.policy_engine.require(operation, block, acting)
        except AccessDenied as denied:
            self.hooks.on_denied(context, operation, block.id, str(denied), error=denied)
            raise

    def _require_access_outside_operation(
        self,
        hook_operation: HookOperation,
        operation: Operation | str,
        block: MemoryBlock,
        *,
        principal: Principal | None = None,
    ) -> None:
        """Require policy access for a call that starts no operation.

        Traversal, listing, and handle creation have no paired callbacks, so a
        refusal opens one operation context to carry the denial and its
        terminal event. Allowed calls stay unobserved.

        Args:
            hook_operation: Logical operation to report the refusal under.
            operation: Policy operation to check.
            block: Block carrying the policy.
            principal: Principal to check. Defaults to this memory's principal.

        Raises:
            AccessDenied: If the block policy does not allow the operation. A
                registry that cannot report the refusal does not change what
                the caller receives.
        """

        acting = self.principal if principal is None else principal
        try:
            self.policy_engine.require(operation, block, acting)
        except AccessDenied as denied:
            try:
                with self.hooks.operation(hook_operation, principal_id=acting.id) as context:
                    self.hooks.on_denied(context, operation, block.id, str(denied), error=denied)
                    raise
            except HookRecursionError as unreportable:
                denied.add_note(f"Refusal could not be reported: {unreportable!r}")
                raise denied

    def _require_raw(self, block_id: str) -> MemoryBlock:
        """Fetch an encoded block or raise `MemoryNotFound`."""

        block = self.backend.get_block(block_id)
        if block is None:
            raise MemoryNotFound(f"Memory block {block_id!r} does not exist.")
        return block

    def _require_stored_link(self, link: MemoryLink) -> MemoryLink:
        """Fetch a canonical link value or raise `MemoryNotFound`."""

        stored = next(
            (
                candidate
                for candidate in self.backend.list_links(link.source_id, direction="out")
                if candidate.id == link.id
            ),
            None,
        )
        if stored is None:
            raise MemoryNotFound(f"Memory link {link.id!r} does not exist.")
        return stored

    def _decode_block(self, block: MemoryBlock) -> MemoryBlock:
        """Decode a stored block value and isolate metadata."""

        decoded = self.codecs.decode(EncodedValue(block.content_type, block.value))
        return block.with_updates(value=decoded, metadata=deepcopy(block.metadata))

    def _can_read_link_endpoints(self, link: MemoryLink) -> bool:
        """Return whether the current principal can read both link endpoints."""

        for block_id in (link.source_id, link.target_id):
            block = self.backend.get_block(block_id)
            if block is None or not self.policy_engine.allows(
                Operation.READ,
                block,
                self.principal,
            ):
                return False
        return True

    def _can_read_mount(self, link: MemoryLink) -> bool:
        """Return whether a mount link is visible to the current principal.

        Mounts may point at routes that are discovered and registered later. In
        that pending state the source policy controls visibility. If the target
        block is already known, its read policy must also allow access.
        """

        if link.relation != LinkRelations.MOUNT:
            return False
        source = self.backend.get_block(link.source_id)
        if source is None or not self.policy_engine.allows(Operation.READ, source, self.principal):
            return False
        target = self.backend.get_block(link.target_id)
        if target is None:
            return True
        return self.policy_engine.allows(Operation.READ, target, self.principal)

    def _filter_visible_results(
        self,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Filter search results by stored block visibility and query scope."""

        return [result for result in results if self._can_return_search_result(query, result)]

    def _can_return_search_result(self, query: MemoryQuery, result: SearchResult) -> bool:
        """Return whether a result is visible for the current query/principal."""

        stored = self.backend.get_block(result.block.id)
        if stored is None:
            return False
        if not self.policy_engine.allows(Operation.SEARCH, stored, self.principal):
            return False
        if not self.policy_engine.allows(Operation.READ, stored, self.principal):
            return False
        return self._result_in_query_scope(query, stored.id)

    def _result_in_query_scope(self, query: MemoryQuery, block_id: str) -> bool:
        """Return whether a block id belongs to the requested search scope."""

        if query.root_id is None or query.scope == Scope.GLOBAL:
            return True
        if query.scope == Scope.SELF:
            return block_id == query.root_id
        if query.scope == Scope.SUBTREE:
            return self._contains_raw(query.root_id, block_id)
        if query.scope == Scope.ANCESTORS:
            return block_id == query.root_id or self._contains_raw(block_id, query.root_id)
        if query.scope == Scope.LINKS:
            if block_id == query.root_id:
                return True
            return any(
                block_id in {link.source_id, link.target_id}
                for link in self.backend.list_links(query.root_id, direction="both")
            )
        return False


def _require_update_hook_invariants(
    proposed: MemoryBlock,
    hooked: MemoryBlock,
) -> None:
    """Require block-update hooks to preserve non-payload fields."""

    stable_fields = (
        "id",
        "type",
        "parent_id",
        "policy",
        "created_at",
        "updated_at",
        "created_by",
        "version",
    )
    changed = [
        field
        for field in stable_fields
        if getattr(hooked, field) != getattr(proposed, field)
    ]
    if changed:
        names = ", ".join(changed)
        raise ValueError(
            "before_update hooks may change only value, metadata, and "
            f"content_type; changed protected fields: {names}."
        )


def _copy_metadata(metadata: object | None) -> dict[str, Any]:
    """Validate and copy one public or hook-produced metadata dictionary."""

    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary or None.")
    try:
        encoded = JsonCodec().encode(metadata)
    except TypeError as error:
        raise TypeError("metadata must contain only strict JSON data.") from error
    return encoded.data


def _require_link_hook_invariants(
    proposed: MemoryLink,
    hooked: MemoryLink,
    *,
    callback: str,
) -> None:
    """Require link hooks to preserve identity and topology."""

    stable_fields = ("id", "source_id", "target_id", "relation", "created_at")
    changed = [
        field
        for field in stable_fields
        if getattr(hooked, field) != getattr(proposed, field)
    ]
    if changed:
        names = ", ".join(changed)
        raise ValueError(
            f"{callback} hooks may change only metadata; changed protected "
            f"fields: {names}."
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
