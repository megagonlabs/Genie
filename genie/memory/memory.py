"""Main memory system interface."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, overload

from genie.backend.base import MemoryBackend
from genie.backend.inmemory import InMemoryBackend
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec
from genie.hooks import HookRegistry, MemoryHook
from genie.memory.block import MemoryBlock, new_memory_id, utc_now
from genie.memory.errors import MemoryNotFound
from genie.memory.links import LinkRelations, MemoryLink
from genie.policy.engine import Operation, PolicyEngine
from genie.policy.policy import MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult
from genie.query.filters import normalize_filters

if TYPE_CHECKING:
    from genie.memory.handle import MemoryHandle


class Memory:
    """Main interface for a Genie memory system.

    `Memory` configures the backend, codecs, hooks, policy engine, and default
    principal. Trusted application code usually keeps this object and passes
    bounded `MemoryHandle` objects to agents. It coordinates parent/child
    ownership, scoped access, links, mounts, search, hooks, and codecs without
    owning application business logic, memory structure, storage layout,
    retrieval strategy, or value meaning.
    """

    def __init__(
        self,
        *,
        backend: MemoryBackend | None = None,
        principal: Principal | str | None = None,
        policy_engine: PolicyEngine | None = None,
        hooks: list[MemoryHook] | HookRegistry | None = None,
        codecs: CodecRegistry | None = None,
    ) -> None:
        """Create the main memory interface.

        Args:
            backend: Backend adapter. Defaults to `InMemoryBackend`.
            principal: Acting principal for operations through this handle.
            policy_engine: Policy evaluator. Defaults to `PolicyEngine`.
            hooks: Hook list or registry.
            codecs: Codec registry. Defaults to a registry containing
                `JsonCodec`.
        """

        self.backend: MemoryBackend = backend or InMemoryBackend()
        self.principal = Principal.coerce(principal)
        self.policy_engine = policy_engine or PolicyEngine()
        if isinstance(hooks, HookRegistry):
            self.hooks = hooks
        else:
            self.hooks = HookRegistry(hooks)
        self.codecs = codecs or CodecRegistry([JsonCodec()])

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
            policy: Optional block-level access policy.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Stored block with decoded value.

        Raises:
            AccessDenied: If the principal cannot write under the parent.
            MemoryNotFound: If the parent id does not exist.
            TypeError: If no codec can encode the value.
        """

        parent_id = _block_id(parent)
        if parent_id is not None:
            parent_block = self._require_raw(parent_id)
            self.policy_engine.require(Operation.WRITE, parent_block, self.principal)
            self.policy_engine.require(Operation.TRAVERSE_DOWN, parent_block, self.principal)
        encoded = self.codecs.encode(value, content_type)
        block = MemoryBlock(
            id=id or new_memory_id(),
            type=type,
            value=encoded.data,
            parent_id=parent_id,
            content_type=encoded.content_type,
            metadata=deepcopy(metadata or {}),
            policy=policy,
            created_by=self.principal.id,
        )
        block = self.hooks.before_put(block)
        if block.parent_id != parent_id:
            raise ValueError("before_put hooks cannot change a block's parent_id.")
        stored = self.backend.put_block(block)
        self.hooks.after_put(stored)
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

        actual_id = _block_id(block_id)
        self.hooks.before_get(actual_id)
        block = self.backend.get_block(actual_id)
        if block is None:
            self.hooks.after_get(None)
            return None
        self.policy_engine.require(Operation.READ, block, self.principal)
        decoded = self._decode_block(block)
        self.hooks.after_get(decoded)
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
            MemoryNotFound: If the block does not exist.
            TypeError: If no codec can encode the value.
        """

        block = self._require_raw(_block_id(block_id))
        self.policy_engine.require(Operation.WRITE, block, self.principal)
        encoded = self.codecs.encode(value, content_type)
        updated = block.with_updates(
            value=encoded.data,
            content_type=encoded.content_type,
            updated_at=utc_now(),
            version=block.version + 1,
        )
        stored = self.backend.update_block(updated)
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
            MemoryNotFound: If the block does not exist.
            TypeError: If `value` is provided and the existing decoded value is
                not a dictionary.
        """

        block = self._require_raw(_block_id(block_id))
        self.policy_engine.require(Operation.WRITE, block, self.principal)
        decoded_value = self.codecs.decode(EncodedValue(block.content_type, block.value))
        if value is not None:
            if not isinstance(decoded_value, dict):
                raise TypeError("Value updates require the existing memory value to be a dict.")
            decoded_value = {**decoded_value, **value}
        encoded = self.codecs.encode(decoded_value, block.content_type)
        updated_metadata = deepcopy({**block.metadata, **(metadata or {})})
        updated = block.with_updates(
            value=encoded.data,
            metadata=updated_metadata,
            updated_at=utc_now(),
            version=block.version + 1,
        )
        stored = self.backend.update_block(updated)
        return self._decode_block(stored)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block and its descendants.

        Recursive delete requires write access to every block that would be
        removed.

        Args:
            block_id: Block or block id to delete.

        Raises:
            AccessDenied: If any affected block denies write access.
            MemoryNotFound: If the block does not exist.
        """

        block = self._require_raw(_block_id(block_id))
        blocks_to_delete = self.backend.descendants(block.id, include_self=True)
        for candidate in blocks_to_delete:
            self.policy_engine.require(Operation.WRITE, candidate, self.principal)
        decoded_blocks = [self._decode_block(candidate) for candidate in blocks_to_delete]
        for decoded_block in decoded_blocks:
            self.hooks.before_delete(decoded_block)
        self.backend.delete_block(block.id)
        for decoded_block in decoded_blocks:
            self.hooks.after_delete(decoded_block)

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
        self.policy_engine.require(Operation.TRAVERSE_DOWN, block, self.principal)
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
        self.policy_engine.require(Operation.TRAVERSE_UP, block, self.principal)
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
        """

        source_block = self._require_raw(_block_id(source))
        target_block = self._require_raw(_block_id(target))
        self.policy_engine.require(Operation.WRITE, source_block, self.principal)
        self.policy_engine.require(Operation.READ, target_block, self.principal)
        return self.backend.add_link(
            MemoryLink(
                source_id=source_block.id,
                target_id=target_block.id,
                relation=relation,
                metadata=dict(metadata or {}),
            )
        )

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
        self.policy_engine.require(Operation.READ, block, self.principal)
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
        """

        return self.add_link(source, target, LinkRelations.MOUNT, metadata=metadata)

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
        self.policy_engine.require(Operation.READ, source_block, self.principal)
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
        self.policy_engine.require(Operation.SEARCH, root_block, self.principal)
        self.policy_engine.require(Operation.TRAVERSE_DOWN, root_block, self.principal)

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
        self.policy_engine.require(Operation.READ, source, self.principal)
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
            ValueError: If the scope is invalid or limit is negative.
        """

        memory_query = self._coerce_query(
            query,
            text=text,
            root=root,
            scope=scope,
            types=types,
            filters=filters,
            limit=limit,
        )
        self._authorize_query(memory_query)
        memory_query = self.hooks.before_search(memory_query)
        # Hooks may return a freshly constructed query. Normalize once more so
        # backends receive canonical filter operators, then check access again
        # so hooks cannot pass through a wider query root or scope.
        memory_query = replace(memory_query, filters=normalize_filters(memory_query.filters))
        self._authorize_query(memory_query)
        raw_results = self.backend.search(memory_query)
        decoded_results = [
            replace(result, block=self._decode_block(result.block))
            for result in raw_results
        ]
        visible_results = self._filter_visible_results(memory_query, decoded_results)
        hooked_results = self.hooks.after_search(memory_query, visible_results)
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

        Returns:
            A `MemoryHandle` backed by this memory system.

        Raises:
            ValueError: If `root` is `None`.
            MemoryNotFound: If `root` does not exist.
        """

        root_id = _block_id(root)
        if root_id is None:
            raise ValueError(
                "Memory.get_handle() requires a root. Use Memory.get_unbounded_handle() "
                "for explicit unbounded access."
            )
        self._require_raw(root_id)
        return self._new_handle(
            root_id=root_id,
            principal=principal,
            default_scope=default_scope,
            allow_up=allow_up,
            allow_global=allow_global,
            allow_mounts=allow_mounts,
        )

    def get_unbounded_handle(
        self,
        *,
        principal: Principal | str | None = None,
        default_scope: Scope | str = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
        allow_mounts: bool = False,
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
        )

    def contains(self, root: str | MemoryBlock, block_id: str | MemoryBlock) -> bool:
        """Return whether one block is inside another block's subtree.

        Args:
            root: Potential subtree root block or id.
            block_id: Candidate block or id.

        Returns:
            `True` when `block_id` is equal to or below `root`.
        """

        return self.backend.contains(_block_id(root), _block_id(block_id))

    def _new_handle(
        self,
        *,
        root_id: str | None,
        principal: Principal | str | None,
        default_scope: Scope | str,
        allow_up: bool,
        allow_global: bool,
        allow_mounts: bool,
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
            types=tuple(types),
            filters=dict(filters or {}),
            limit=limit,
        )

    def _authorize_query(self, query: MemoryQuery) -> None:
        """Check access required by a query root and scope."""

        if query.root_id is None:
            return
        root = self._require_raw(query.root_id)
        self.policy_engine.require(Operation.SEARCH, root, self.principal)
        if query.scope == Scope.SUBTREE:
            self.policy_engine.require(Operation.TRAVERSE_DOWN, root, self.principal)
        elif query.scope == Scope.ANCESTORS:
            self.policy_engine.require(Operation.TRAVERSE_UP, root, self.principal)
        elif query.scope == Scope.LINKS:
            self.policy_engine.require(Operation.READ, root, self.principal)

    def _require_raw(self, block_id: str) -> MemoryBlock:
        """Fetch an encoded block or raise `MemoryNotFound`."""

        block = self.backend.get_block(block_id)
        if block is None:
            raise MemoryNotFound(f"Memory block {block_id!r} does not exist.")
        return block

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
            return self.backend.contains(query.root_id, block_id)
        if query.scope == Scope.ANCESTORS:
            return block_id == query.root_id or self.backend.contains(block_id, query.root_id)
        if query.scope == Scope.LINKS:
            if block_id == query.root_id:
                return True
            return any(
                block_id in {link.source_id, link.target_id}
                for link in self.backend.list_links(query.root_id, direction="both")
            )
        return False


@overload
def _block_id(block: str | MemoryBlock) -> str: ...


@overload
def _block_id(block: None) -> None: ...


def _block_id(block: str | MemoryBlock | None) -> str | None:
    """Return a block id from a block object, id string, or `None`."""

    if isinstance(block, MemoryBlock):
        return block.id
    return block
