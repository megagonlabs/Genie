"""Main interface of the memory system."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any, Literal

from genie.backend.base import MemoryBackend
from genie.backend.in_memory import InMemoryBackend
from genie.backend.structure_capabilities import AcyclicLinkStore
from genie.codecs import MAX_NESTING_DEPTH, CodecRegistry, EncodedValue, JsonCodec
from genie.hooks import (
    HookContext,
    HookOperation,
    HookRegistry,
    MemoryHook,
    Traversal,
    TraversalKind,
    freeze_attributes,
)
from genie.memory._validation import require_non_empty_str
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

    - **Configuration:** the backend, codecs, hooks, policy engine, default
      principal, default policy, and the attributes that hooks see.
    - **Coordination:** parent and child ownership, scoped access, links,
      mounts, search, hooks, and codecs.
    - **Not in scope:** the application business logic, memory structure,
      storage layout, retrieval strategy, and value meaning.
    - **Usual use:** trusted application code keeps this object and gives
      bounded `MemoryHandle` objects to agents.

    Attributes:
        backend: Adapter that stores blocks and links.
        principal: Principal for all operations through this object.
        policy_engine: Evaluator that checks each block access.
        codecs: Registry that encodes and decodes block values.
        default_policy: Policy for blocks that are created without one.
        attributes: Read-only string labels on each hook context that this
            memory opens.
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
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Create the main memory interface.

        Args:
            backend: Backend adapter. Defaults to `InMemoryBackend`.
            principal: Acting principal for operations through this memory.
                Defaults to the `system` principal.
            policy_engine: Policy evaluator. Defaults to `PolicyEngine`.
            hooks: Hook list or registry. `Memory.hooks` is read-only after
                this call. So for mandatory hooks that the rest of the process
                cannot detach, give a registry.
            codecs: Codec registry. Defaults to a registry with only
                `JsonCodec`.
            default_policy: Policy for new blocks when `put(..., policy=None)`.
                Defaults to `MemoryPolicy.owner_private()`.
            attributes: String labels on each hook context that this memory
                opens, such as a service, tenant, or deployment name.
                Operations inside a `HookRegistry` scope also inherit the
                attributes of that scope. These attributes win on conflict.

        Raises:
            TypeError: If a configured backend, principal, policy engine,
                codec registry, default policy, or attribute has the wrong
                type.
            ValueError: If `principal` is an empty id string or the same hook
                appears twice in `hooks`.
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
        self._hooks = hooks if isinstance(hooks, HookRegistry) else HookRegistry(hooks)
        self.codecs = CodecRegistry([JsonCodec()]) if codecs is None else codecs
        self.default_policy = (
            MemoryPolicy.owner_private() if default_policy is None else default_policy
        )
        self.attributes = freeze_attributes(attributes)

    @property
    def hooks(self) -> HookRegistry:
        """Registry through which each operation of this memory dispatches.

        - **Fixed registry:** the registry stays the same for the life of the
          memory.
        - **Changeable contents:** `HookRegistry.register` and
          `HookRegistry.unregister` change which hooks run. So code cannot
          detach a mandatory hook with a new registry.
        """

        return self._hooks

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
            metadata: Optional metadata for filters or indexes.
            policy: Optional block-level access policy. Without one, the block
                gets the `default_policy` of the memory.
            content_type: Optional explicit codec content type.
            id: Optional caller-provided block id.

        Returns:
            Stored block with decoded value.

        Raises:
            AccessDenied: If the principal cannot `write` or `traverse_down`
                the parent.
            MemoryConflict: If a block with the same `id` already exists.
            MemoryNotFound: If the parent id does not exist.
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

        with self._operation(HookOperation.PUT) as context:
            require_non_empty_str(type, field_name="type")
            if id is not None:
                require_non_empty_str(id, field_name="id")
            if policy is not None and not isinstance(policy, MemoryPolicy):
                raise TypeError("policy must be a MemoryPolicy or None.")
            parent_id = _block_id(parent) if parent is not None else None
            if parent_id is not None:
                parent_block = self._require_raw(parent_id)
                self._require_access(context, Operation.WRITE, parent_block)
                self._require_access(context, Operation.TRAVERSE_DOWN, parent_block)
            encoded = self.codecs.encode(value, content_type)
            proposed = MemoryBlock(
                id=new_memory_id() if id is None else id,
                type=type,
                value=encoded.data,
                parent_id=parent_id,
                content_type=encoded.content_type,
                metadata=_copy_metadata(metadata),
                policy=policy if policy is not None else self.default_policy,
                created_by=self.principal.id,
            )
            block = self.hooks.before_put(context, proposed)
            block = block.with_updates(metadata=_copy_metadata(block.metadata))
            _require_block_hook_invariants(proposed, block, callback="before_put", mutable_fields=_PUT_HOOK_FIELDS)
            if not isinstance(block.policy, MemoryPolicy):
                raise TypeError("before_put hooks must set policy to a MemoryPolicy.")
            stored = self.backend.put_block(block)
            self.hooks.after_put(context, stored)
            return self._decode_block(stored)

    def get(self, block_id: str | MemoryBlock) -> MemoryBlock | None:
        """Get a memory block by id.

        Args:
            block_id: Block or block id.

        Returns:
            Decoded block, or `None` when the block does not exist.

        Raises:
            AccessDenied: If the principal cannot read the block.
            TypeError: If `block_id` is not a block or string id, or no codec
                can decode the stored value.
            ValueError: If `block_id` is empty.
        """

        with self._operation(HookOperation.GET) as context:
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
        """Replace the value of a block.

        Args:
            block_id: Block or block id to update.
            value: New user-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the principal cannot write and read the block.
                The `write` check runs first. `read` is also needed, because
                the call returns the stored block.
            MemoryConflict: If another writer changes the block before commit.
            MemoryNotFound: If the block does not exist.
            TypeError: If `block_id` is not a block or string id, or no codec
                can encode the value.
            ValueError: If `block_id` is empty, a JSON value nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_update` hook changes a field other than the value,
                metadata, or content type.
        """

        with self._operation(HookOperation.SET) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.WRITE, block)
            self._require_access(context, Operation.READ, block)
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
            _require_block_hook_invariants(proposed, updated, callback="before_update", mutable_fields=_UPDATE_HOOK_FIELDS)
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
            value: Dictionary fields to merge into the current decoded value.
            metadata: Metadata fields to merge.

        Returns:
            Updated decoded block.

        Raises:
            AccessDenied: If the principal cannot write and read the block.
                The `write` check runs first. `read` is also needed, because
                the call returns the stored block.
            MemoryConflict: If another writer changes the block before commit.
            MemoryNotFound: If the block does not exist.
            TypeError: If `block_id` is not a block or string id, `value` is
                provided and the existing decoded value is not a dictionary,
                or metadata is not a strict JSON dictionary.
            ValueError: If `block_id` is empty, a JSON value or the metadata
                nests lists and dictionaries more than `MAX_NESTING_DEPTH`
                levels deep, or a `before_update` hook changes a field other
                than the value, metadata, or content type.
        """

        with self._operation(HookOperation.UPDATE) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.WRITE, block)
            self._require_access(context, Operation.READ, block)
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
            _require_block_hook_invariants(proposed, updated, callback="before_update", mutable_fields=_UPDATE_HOOK_FIELDS)
            stored = self.backend.update_block(updated, expected_version=block.version)
            self.hooks.after_update(context, block, stored)
            return self._decode_block(stored)

    def delete(self, block_id: str | MemoryBlock) -> None:
        """Delete a block and its descendants.

        - **Target only:** authorization checks only the target block. `write`
          on the target removes everything that the target owns. Genie does
          not check the policies of the descendants.
        - **Why:** ownership already gives this power. A parent with `write`
          already controls its subtree. If each descendant had its own
          protection, one descendant without `write` could stop the delete of
          the whole branch.
        - **To outlive the parent:** a descendant stays outside the subtree,
          with a link or a mount instead of ownership.

        Args:
            block_id: Block or block id to delete.

        Raises:
            AccessDenied: If the target block denies write access.
            MemoryConflict: If the checked subtree changes before commit.
            MemoryNotFound: If the block does not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
        """

        with self._operation(HookOperation.DELETE) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.WRITE, block)
            blocks_to_delete = self.backend.descendants(block.id, include_self=True)
            decoded_blocks = [self._decode_block(candidate) for candidate in blocks_to_delete]
            for decoded_block in decoded_blocks:
                self.hooks.before_delete(context, decoded_block)
            self.backend.delete_block(
                block.id,
                expected_versions={candidate.id: candidate.version for candidate in blocks_to_delete},
            )
            self.hooks.after_delete(context, *decoded_blocks)

    def children(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
        """Return the readable direct children of a block.

        Args:
            block_id: Parent block or id.

        Returns:
            Decoded child blocks that the principal can read.

        Raises:
            AccessDenied: If the principal cannot traverse downward from the
                parent.
            MemoryNotFound: If the parent does not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
        """

        with self._operation(HookOperation.CHILDREN) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.TRAVERSE_DOWN, block)
            traversal = Traversal(kind=TraversalKind.CHILDREN, origin_id=block.id)
            self.hooks.before_traverse(context, traversal)
            children = [
                self._decode_block(child)
                for child in self.backend.children(block.id)
                if self.policy_engine.allows(Operation.READ, child, self.principal)
            ]
            self.hooks.after_traverse(context, replace(traversal, blocks=tuple(children)))
            return children

    def ancestors(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
        """Return the readable ancestors of a block.

        Args:
            block_id: Block or id whose parents to traverse.

        Returns:
            Decoded ancestor blocks that the principal can read, nearest parent
            first.

        Raises:
            AccessDenied: If the principal cannot traverse upward from the
                block.
            MemoryNotFound: If the block does not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty.
        """

        with self._operation(HookOperation.ANCESTORS) as context:
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.TRAVERSE_UP, block)
            traversal = Traversal(kind=TraversalKind.ANCESTORS, origin_id=block.id)
            self.hooks.before_traverse(context, traversal)
            ancestors = [
                self._decode_block(ancestor)
                for ancestor in self.backend.ancestors(block.id)
                if self.policy_engine.allows(Operation.READ, ancestor, self.principal)
            ]
            self.hooks.after_traverse(context, replace(traversal, blocks=tuple(ancestors)))
            return ancestors

    def add_link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a typed relationship between 2 blocks, with no ownership.

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
            MemoryConflict: If an earlier `add_acyclic_link` protects
                `relation`. Then only `add_acyclic_link` can add links with
                that relation.
            MemoryNotFound: If an endpoint does not exist.
            TypeError: If `source` or `target` is not a block or string id,
                `relation` is not a string, or metadata is not a strict JSON
                dictionary.
            ValueError: If an endpoint id or `relation` is empty, metadata
                nests lists and dictionaries more than `MAX_NESTING_DEPTH`
                levels deep, or a `before_add_link` hook changes link identity
                or topology.
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

        The link identity and topology do not change. The returned object is
        a replacement value with the state that the backend stored.

        Args:
            link: Existing link returned by `add_link` or `list_links`.
            metadata: Metadata fields to merge into the stored link metadata.

        Returns:
            Stored replacement link.

        Raises:
            AccessDenied: If the principal cannot write and read the source, or
                read the target. `read` on the source is needed, because the
                call returns the stored link. For a pending mount with no
                registered target, only the source policy applies.
            MemoryNotFound: If the link or a non-mount endpoint does not exist.
            TypeError: If `link` is not a `MemoryLink`, or metadata is not a
                strict JSON dictionary.
            ValueError: If metadata nests lists and dictionaries more than
                `MAX_NESTING_DEPTH` levels deep, or a `before_update_link` hook
                changes link identity or topology.
        """

        with self._operation(HookOperation.UPDATE_LINK) as context:
            stored = self._require_stored_link(link)
            source = self._require_raw(stored.source_id)
            self._require_access(context, Operation.WRITE, source)
            self._require_access(context, Operation.READ, source)
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
        """Create a typed link in one atomic step, unless the link makes a cycle.

        - **Cycle check:** it includes all stored links with the exact same
          relation.
        - **One operation:** the backend does the reachability check and the
          insert as one operation.
        - **Protected relation:** after the first acyclic link, a plain
          `add_link` with the same relation raises `MemoryConflict`.

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
            MemoryConflict: If the current links and the new link contain a
                cycle.
            MemoryNotFound: If an endpoint does not exist.
            TypeError: If `source` or `target` is not a block or string id,
                `relation` is not a string, or metadata is not a strict JSON
                dictionary.
            UnsupportedOperation: If the backend cannot insert DAG links
                atomically.
            ValueError: If an endpoint id or `relation` is empty, `relation`
                is the reserved mount relation, metadata nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_add_link` hook changes link identity or topology.
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
        """Authorize one link insert, dispatch its hooks, and store it.

        Args:
            source: Source block or id.
            target: Target block or id.
            relation: Relation type stored on the link.
            metadata: Optional relationship metadata.
            hook_operation: Logical operation that reports the insert.
            acyclic: If `True`, the backend rejects a link that closes a cycle
                over `relation`.

        Returns:
            Stored memory link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target.
            MemoryConflict: If `acyclic` is true and the link would form a
                cycle.
            MemoryNotFound: If either endpoint is missing.
            TypeError: If `source` or `target` is not a block or string id,
                `relation` is not a string, or metadata is not a strict JSON
                dictionary.
            UnsupportedOperation: If `acyclic` is true and the backend lacks
                atomic DAG insertion.
            ValueError: If an endpoint id or `relation` is empty, metadata
                nests lists and dictionaries more than `MAX_NESTING_DEPTH`
                levels deep, or a `before_add_link` hook changes link identity
                or topology.
        """

        with self._operation(hook_operation) as context:
            require_non_empty_str(relation, field_name="relation")
            source_id = _block_id(source)
            target_id = _block_id(target)
            source_block = self._require_raw(source_id)
            target_block = self._require_raw(target_id)
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

        The call returns a link only when the principal can read both
        endpoints.

        Args:
            block_id: Block or id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            AccessDenied: If the principal cannot read `block_id`.
            MemoryNotFound: If `block_id` does not exist.
            TypeError: If `block_id` is not a block or string id, or
                `relation` is not a string or `None`.
            ValueError: If `block_id` or `relation` is empty, or `direction`
                is not valid.
        """

        with self._operation(HookOperation.LIST_LINKS) as context:
            if relation is not None:
                require_non_empty_str(relation, field_name="relation")
            block = self._require_raw(_block_id(block_id))
            self._require_access(context, Operation.READ, block)
            traversal = Traversal(
                kind=TraversalKind.LINKS,
                origin_id=block.id,
                relation=relation,
                direction=direction,
            )
            self.hooks.before_traverse(context, traversal)
            links = [
                link
                for link in self.backend.list_links(
                    block.id,
                    relation=relation,
                    direction=direction,
                )
                if self._can_read_link_endpoints(link)
            ]
            self.hooks.after_traverse(context, replace(traversal, links=tuple(links)))
            return links

    def add_mount(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Create a symbolic mount from one block to another block.

        - **Storage:** a mount is a typed link with the reserved
          `LinkRelations.MOUNT` relation.
        - **No ownership:** it does not make the target a child of the source.
          Subtree traversal and recursive delete do not follow it.

        Args:
            source: Source block or id where the mount link starts.
            target: Mounted target root block or id.
            metadata: Optional mount metadata.

        Returns:
            Stored mount link.

        Raises:
            AccessDenied: If the principal cannot write the source or read the
                target.
            MemoryNotFound: If an endpoint does not exist.
            TypeError: If `source` or `target` is not a block or string id, or
                metadata is not a strict JSON dictionary.
            ValueError: If an endpoint id is empty, metadata nests lists and
                dictionaries more than `MAX_NESTING_DEPTH` levels deep, or a
                `before_add_link` hook changes link identity or topology.
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
            Readable outgoing mount links. A mount whose target route is not
            registered yet comes back as a pending symbolic reference. Its
            resolution returns `None` until the target is available.

        Raises:
            AccessDenied: If the principal cannot read `source`.
            MemoryNotFound: If `source` does not exist.
            TypeError: If `source` is not a block or string id.
            ValueError: If `source` is empty.
        """

        with self._operation(HookOperation.LIST_MOUNTS) as context:
            source_block = self._require_raw(_block_id(source))
            self._require_access(context, Operation.READ, source_block)
            traversal = Traversal(
                kind=TraversalKind.MOUNTS,
                origin_id=source_block.id,
                relation=LinkRelations.MOUNT,
                direction="out",
            )
            self.hooks.before_traverse(context, traversal)
            mounts = [
                link
                for link in self.backend.list_links(
                    source_block.id,
                    relation=LinkRelations.MOUNT,
                    direction="out",
                )
                if self._can_read_mount(link)
            ]
            self.hooks.after_traverse(context, replace(traversal, links=tuple(mounts)))
            return mounts

    def discover_mounts(
        self,
        root: str | MemoryBlock,
        *,
        include_self: bool = True,
    ) -> list[MemoryLink]:
        """Discover symbolic mounts attached to blocks in a regular subtree.

        - **Scan:** discovery scans the normal parent and child subtree below
          `root`. It returns the readable outgoing mount links of those source
          blocks.
        - **No resolution:** it does not resolve mount targets, and it does not
          go into the mounted target trees.

        Args:
            root: Subtree root block or id to scan.
            include_self: If `True`, the result includes the mounts on `root`
                itself.

        Returns:
            Readable mount links whose source is inside the scanned subtree.

        Raises:
            AccessDenied: If the principal cannot search and traverse downward
                from `root`.
            MemoryNotFound: If `root` does not exist.
            TypeError: If `root` is not a block or string id.
            ValueError: If `root` is empty.
        """

        with self._operation(HookOperation.DISCOVER_MOUNTS) as context:
            root_id = _block_id(root)
            root_block = self._require_raw(root_id)
            self._require_access(context, Operation.SEARCH, root_block)
            self._require_access(context, Operation.TRAVERSE_DOWN, root_block)
            traversal = Traversal(
                kind=TraversalKind.SUBTREE_MOUNTS,
                origin_id=root_block.id,
                relation=LinkRelations.MOUNT,
                direction="out",
            )
            self.hooks.before_traverse(context, traversal)
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
            self.hooks.after_traverse(context, replace(traversal, links=tuple(mounts)))
            return mounts

    def resolve_mount(self, mount: MemoryLink) -> MemoryBlock | None:
        """Resolve a mount link to its target block.

        Args:
            mount: Mount link from `add_mount` or `list_mounts`.

        Returns:
            Decoded target block, or `None` if the mount or the target no
            longer exists.

        Raises:
            AccessDenied: If the principal cannot read the source or target.
            MemoryNotFound: If the source block no longer exists.
            TypeError: If `mount` is not a `MemoryLink`.
            ValueError: If `mount` is not a mount link.
        """

        if not isinstance(mount, MemoryLink):
            raise TypeError("mount must be a MemoryLink.")
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
            limit: Maximum result count. The backend applies this limit before
                the policy and scope filters run. So a search can return fewer
                results than the limit, also when more visible blocks match. A
                caller that needs a full page asks for a larger limit and cuts
                the result itself.

        Returns:
            Search results with decoded blocks.

        Raises:
            AccessDenied: If the principal cannot search or traverse from the
                query root.
            MemoryNotFound: If the query root does not exist.
            TypeError: If the text, root, types, filters, or limit arguments
                have the wrong type.
            ValueError: If the scope is invalid, the limit is negative, the
                root id is empty, a non-global scope has no root, or a filter
                uses an unknown operator.
        """

        _, results = self._search(
            query,
            text=text,
            root=root,
            scope=scope,
            types=types,
            filters=filters,
            limit=limit,
        )
        return results

    def _search(
        self,
        query: MemoryQuery | str | None,
        *,
        text: str | None,
        root: str | MemoryBlock | None,
        scope: Scope | str,
        types: list[str] | tuple[str, ...],
        filters: dict[str, Any] | None,
        limit: int,
        boundary: Callable[[HookContext, MemoryQuery], None] | None = None,
    ) -> tuple[MemoryQuery, list[SearchResult]]:
        """Run `search`, and also return the query that the backend ran.

        Args:
            query: Optional `MemoryQuery` or text query string.
            text: Text query when `query` is not a string.
            root: Optional root block or id for scoped search.
            scope: Search scope.
            types: Optional block types to include.
            filters: Optional metadata or block-field filters.
            limit: Maximum result count.
            boundary: Optional extra check for the query that `before_search`
                hooks return. It runs inside this operation, before the query
                is authorized again, and it raises `AccessDenied` to refuse.
                `MemoryHandle` gives one, so a hook cannot widen a search past
                the root or the capabilities of the handle.

        Returns:
            The query after `before_search` hooks, and the visible results.
        """

        with self._operation(HookOperation.SEARCH) as context:
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
            # Hooks can return a new query. Normalize it again, so backends get
            # canonical filter operators. Then check access again, so hooks
            # cannot pass a wider query root or scope.
            memory_query = replace(memory_query, filters=normalize_filters(memory_query.filters))
            if boundary is not None:
                boundary(context, memory_query)
            self._authorize_query(context, memory_query)
            raw_results = self.backend.search(memory_query)
            decoded_results = [
                replace(result, block=self._decode_block(result.block))
                for result in raw_results
            ]
            # One visibility verdict for each block id for the whole call. Both
            # filter passes share it, so each block is read only once.
            verdicts: dict[str, bool] = {}
            visible_results = self._filter_visible_results(
                memory_query,
                decoded_results,
                verdicts,
            )
            hooked_results = self.hooks.after_search(
                context,
                memory_query,
                visible_results,
            )
            return memory_query, self._filter_visible_results(memory_query, hooked_results, verdicts)

    def require_access(self, operation: Operation | str, block_id: str | MemoryBlock) -> None:
        """Raise `AccessDenied` if this principal cannot perform an operation on a block.

        - **No change:** the block does not change, and the call returns nothing.
        - **Same check as operations:** each operation runs this check before
          it touches the block. So code that refuses early can ask for it
          directly. An example is a structure facade that changes membership.
        - **Hooks:** a refusal goes to `on_denied` under
          `HookOperation.REQUIRE_ACCESS`. An allowed check is not reported.

        Args:
            operation: Built-in `Operation`, or the name of a custom operation
                configured on the policy engine.
            block_id: Block or block id that has the policy.

        Raises:
            AccessDenied: If the block policy does not allow the operation.
            MemoryNotFound: If the block does not exist.
            TypeError: If `block_id` is not a block or string id.
            ValueError: If `block_id` is empty, or `operation` is neither
                built-in nor configured as a custom operation.
        """

        block = self._require_raw(_block_id(block_id))
        self._require_access_outside_operation(HookOperation.REQUIRE_ACCESS, operation, block)

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
    ) -> MemoryHandle:
        """Create a bounded memory handle to give to agents.

        Args:
            root: Root block or id that is the boundary of the handle.
            principal: Principal for operations through the handle. Defaults to
                the principal of this `Memory`.
            default_scope: Default search scope of the returned handle.
            allow_up: Whether ancestor traversal and ancestor search are
                allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether the handle can create, list, and resolve
                symbolic mounts whose source is inside the handle.
            allow_policy_override: Whether the handle can give explicit block
                policies to `put`. For untrusted agent code, it stays `False`.

        Returns:
            A `MemoryHandle` backed by this memory system.

        Raises:
            AccessDenied: If the handle principal cannot read `root`.
            MemoryNotFound: If `root` does not exist.
            TypeError: If `root` is not a block or string id, or `principal`
                is not a principal, id string, or `None`.
            ValueError: If `root` is `None` or empty, the scope name is
                unknown, or `default_scope` requires a capability the new
                handle does not enable.
        """

        if root is None:
            raise ValueError(
                "Memory.get_handle() requires a root. Use Memory.get_unbounded_handle() "
                "for explicit unbounded access."
            )
        root_id = _block_id(root)
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
    ) -> MemoryHandle:
        """Create an explicit unbounded memory handle.

        - **No root:** an unbounded handle has no default subtree root.
        - **Other limits stay:** principal policies and options such as
          `allow_global` still limit it.
        - **Search root:** a non-global search through an unbounded handle
          needs an explicit root.

        Args:
            principal: Principal for operations through the handle. Defaults to
                the principal of this `Memory`.
            default_scope: Default search scope of the returned handle.
            allow_up: Whether ancestor traversal and ancestor search are
                allowed.
            allow_global: Whether global search is allowed.
            allow_mounts: Whether the handle can create, list, and resolve
                symbolic mounts whose source is visible through the handle.
            allow_policy_override: Whether the handle can give explicit block
                policies to `put`. For untrusted agent code, it stays `False`.

        Returns:
            An unbounded `MemoryHandle` backed by this memory system.

        Raises:
            TypeError: If `principal` is not a principal, id string, or `None`.
            ValueError: If the scope name is unknown or `default_scope`
                requires a capability the new handle does not enable.
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
            `True` when the candidate is the root or is below it, and its read
            policy allows the current principal. A candidate that does not
            exist or that the principal cannot read gives `False`.

        Raises:
            AccessDenied: If downward traversal from the root is denied.
            MemoryNotFound: If the root does not exist.
            TypeError: If `root` or `block_id` is not a block or string id.
            ValueError: If `root` or `block_id` is empty.
        """

        with self._operation(HookOperation.CONTAINS) as context:
            root_id = _block_id(root)
            candidate_id = _block_id(block_id)
            root_block = self._require_raw(root_id)
            self._require_access(context, Operation.TRAVERSE_DOWN, root_block)
            traversal = Traversal(
                kind=TraversalKind.CONTAINS,
                origin_id=root_block.id,
                target_id=candidate_id,
            )
            self.hooks.before_traverse(context, traversal)
            candidate = self.backend.get_block(candidate_id)
            contained = (
                candidate is not None
                and self._contains_raw(root_id, candidate_id)
                and self.policy_engine.allows(Operation.READ, candidate, self.principal)
            )
            self.hooks.after_traverse(context, replace(traversal, contained=contained))
            return contained

    def _contains_raw(self, root_id: str, block_id: str) -> bool:
        """Return ownership membership from the backend, with no policy check."""

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
    ) -> MemoryHandle:
        """Create a `MemoryHandle` after the root check is complete."""

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
        """Change friendly search arguments into a `MemoryQuery`."""

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
        """Raise an error if the principal does not have the access that the query root and scope need."""

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

    def _operation(
        self,
        operation: HookOperation,
        *,
        principal: Principal | None = None,
    ) -> AbstractContextManager[HookContext]:
        """Open the causal context of one operation that this memory performs.

        Each operation reports the acting principal and the attributes of this
        memory. So the trail can tell a `Memory` that a handle derives apart
        from the trusted memory whose state it shares.

        Args:
            operation: Logical operation.
            principal: Acting principal. Defaults to the principal of this
                memory.

        Returns:
            Context manager that yields the `HookContext` of the operation.
        """

        acting = self.principal if principal is None else principal
        return self.hooks.operation(
            operation,
            principal_id=acting.id,
            attributes=self.attributes,
        )

    def _require_access(
        self,
        context: HookContext,
        operation: Operation | str,
        block: MemoryBlock,
        *,
        principal: Principal | None = None,
    ) -> None:
        """Check policy access inside an operation, and report refusals.

        Args:
            context: Active operation context.
            operation: Policy operation to check.
            block: Block that has the policy.
            principal: Principal to check. Defaults to the principal of this
                memory.

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
        """Check policy access for a call that starts no operation.

        - **Refusals only:** mount resolution, handle creation, and
          `require_access` have no paired callbacks. So a refusal opens one
          operation context for the denial and its terminal event.
        - **Allowed calls:** hooks do not see them.

        Args:
            hook_operation: Logical operation that reports the refusal.
            operation: Policy operation to check.
            block: Block that has the policy.
            principal: Principal to check. Defaults to the principal of this
                memory.

        Raises:
            AccessDenied: If the block policy does not allow the operation. If
                the registry cannot report the refusal, the caller still gets
                the same error, with a note.
        """

        acting = self.principal if principal is None else principal
        try:
            self.policy_engine.require(operation, block, acting)
        except AccessDenied as denied:
            try:
                with self._operation(hook_operation, principal=acting) as context:
                    self.hooks.on_denied(context, operation, block.id, str(denied), error=denied)
                    raise
            except HookRecursionError as unreportable:
                denied.add_note(f"Refusal could not be reported: {unreportable!r}")
                raise denied

    def _require_raw(self, block_id: str) -> MemoryBlock:
        """Get an encoded block, or raise `MemoryNotFound`."""

        block = self.backend.get_block(block_id)
        if block is None:
            raise MemoryNotFound(f"Memory block {block_id!r} does not exist.")
        return block

    def _require_stored_link(self, link: MemoryLink) -> MemoryLink:
        """Get the stored link value, or raise `MemoryNotFound`."""

        if not isinstance(link, MemoryLink):
            raise TypeError("link must be a MemoryLink.")
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
        """Decode a stored block value, and copy the metadata."""

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

        - **Pending mounts:** a mount can point at a route that a caller finds
          and registers later. In that pending state, the source policy
          controls visibility.
        - **Known target:** if the target block is already known, its read
          policy also has to allow access.
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
        verdicts: dict[str, bool] | None = None,
    ) -> list[SearchResult]:
        """Filter search results by stored block visibility and query scope.

        - **Shared verdicts:** `search` gives one `verdicts` mapping to both of
          its filter passes. So the pass after `after_search` does not read
          each block a second time.
        - **Same strictness:** a reused verdict is as strict as a new check.
          Each decision in `_can_return_search_result` comes from the block id
          and the stored block that it names, never from the rest of a result.
        - **Consequence:** if a hook deletes the block of a result inside
          `after_search`, that result stays on the page.

        Args:
            query: Query that gave the results.
            results: Results to filter.
            verdicts: Visibility decisions from earlier in this operation, by
                block id.

        Returns:
            The results that the acting principal can get.
        """

        decisions = {} if verdicts is None else verdicts
        visible: list[SearchResult] = []
        for result in results:
            block_id = result.block.id
            if block_id not in decisions:
                decisions[block_id] = self._can_return_search_result(query, result)
            if decisions[block_id]:
                visible.append(result)
        return visible

    def _can_return_search_result(self, query: MemoryQuery, result: SearchResult) -> bool:
        """Return whether a result is visible for the current query and principal."""

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


_UPDATE_HOOK_FIELDS = ("value", "metadata", "content_type")
_PUT_HOOK_FIELDS = (*_UPDATE_HOOK_FIELDS, "policy")


def _require_block_hook_invariants(
    proposed: MemoryBlock,
    hooked: MemoryBlock,
    *,
    callback: str,
    mutable_fields: tuple[str, ...],
) -> None:
    """Raise `ValueError` if a block hook changes a field that its callback does not allow.

    All other `MemoryBlock` fields are protected, also fields that a later
    version adds.
    """

    changed = [
        field.name
        for field in fields(MemoryBlock)
        if field.name not in mutable_fields
        and getattr(hooked, field.name) != getattr(proposed, field.name)
    ]
    if changed:
        allowed = f"{', '.join(mutable_fields[:-1])}, and {mutable_fields[-1]}"
        names = ", ".join(changed)
        raise ValueError(
            f"{callback} hooks may change only {allowed}; changed protected "
            f"fields: {names}."
        )


def _copy_metadata(metadata: object | None) -> dict[str, Any]:
    """Check and copy one metadata dictionary from a caller or a hook."""

    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary or None.")
    try:
        encoded = JsonCodec().encode(metadata)
    except TypeError as error:
        raise TypeError("metadata must contain only strict JSON data.") from error
    except ValueError as error:
        raise ValueError(f"metadata must not nest lists and dictionaries more than {MAX_NESTING_DEPTH} levels deep.") from error
    return encoded.data


def _require_link_hook_invariants(
    proposed: MemoryLink,
    hooked: MemoryLink,
    *,
    callback: str,
) -> None:
    """Raise `ValueError` if a link hook changes the identity or the topology of the link."""

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
