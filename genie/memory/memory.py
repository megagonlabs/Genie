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
from genie.memory.links import MemoryLink
from genie.policy.engine import Operation, PolicyEngine
from genie.policy.policy import MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult
from genie.query.filters import normalize_filters

if TYPE_CHECKING:
    from genie.memory.scope import MemoryScope


class Memory:
    """Public API for working with Genie memory."""

    def __init__(
        self,
        *,
        backend: MemoryBackend | None = None,
        principal: Principal | str | None = None,
        policy_engine: PolicyEngine | None = None,
        hooks: list[MemoryHook] | HookRegistry | None = None,
        codecs: CodecRegistry | None = None,
    ) -> None:
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
        stored = self.backend.put_block(block)
        self.hooks.after_put(stored)
        return self._decode_block(stored)

    def get(self, block_id: str | MemoryBlock) -> MemoryBlock | None:
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

    def patch(
        self,
        block_id: str | MemoryBlock,
        *,
        value: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryBlock:
        block = self._require_raw(_block_id(block_id))
        self.policy_engine.require(Operation.WRITE, block, self.principal)
        decoded_value = self.codecs.decode(EncodedValue(block.content_type, block.value))
        if value is not None:
            if not isinstance(decoded_value, dict):
                raise TypeError("Value patches require the existing memory value to be a dict.")
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
        block = self._require_raw(_block_id(block_id))
        blocks_to_delete = self.backend.descendants(block.id, include_self=True)
        for candidate in blocks_to_delete:
            self.policy_engine.require(Operation.WRITE, candidate, self.principal)
        decoded_block = self._decode_block(block)
        self.hooks.before_delete(decoded_block)
        self.backend.delete_block(block.id)
        self.hooks.after_delete(decoded_block)

    def children(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
        block = self._require_raw(_block_id(block_id))
        self.policy_engine.require(Operation.TRAVERSE_DOWN, block, self.principal)
        return [
            self._decode_block(child)
            for child in self.backend.children(block.id)
            if self.policy_engine.allows(Operation.READ, child, self.principal)
        ]

    def ancestors(self, block_id: str | MemoryBlock) -> list[MemoryBlock]:
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
        block = self._require_raw(_block_id(block_id))
        self.policy_engine.require(Operation.READ, block, self.principal)
        return [
            link
            for link in self.backend.list_links(block.id, relation=relation, direction=direction)
            if self._can_read_link_endpoints(link)
        ]

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
        memory_query = replace(memory_query, filters=normalize_filters(memory_query.filters))
        raw_results = self.backend.search(memory_query)
        decoded_results = [
            replace(result, block=self._decode_block(result.block))
            for result in raw_results
            if self.policy_engine.allows(Operation.SEARCH, result.block, self.principal)
            and self.policy_engine.allows(Operation.READ, result.block, self.principal)
        ]
        return self.hooks.after_search(memory_query, decoded_results)

    def scope(
        self,
        *,
        root: str | MemoryBlock | None = None,
        principal: Principal | str | None = None,
        default_scope: Scope | str = Scope.SUBTREE,
        allow_up: bool = False,
        allow_global: bool = False,
    ) -> "MemoryScope":
        from genie.memory.scope import MemoryScope

        return MemoryScope(
            memory=self,
            root_id=_block_id(root) if root is not None else None,
            principal=Principal.coerce(principal) if principal is not None else self.principal,
            default_scope=Scope(default_scope),
            allow_up=allow_up,
            allow_global=allow_global,
        )

    def contains(self, root: str | MemoryBlock, block_id: str | MemoryBlock) -> bool:
        return self.backend.contains(_block_id(root), _block_id(block_id))

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
        block = self.backend.get_block(block_id)
        if block is None:
            raise MemoryNotFound(f"Memory block {block_id!r} does not exist.")
        return block

    def _decode_block(self, block: MemoryBlock) -> MemoryBlock:
        decoded = self.codecs.decode(EncodedValue(block.content_type, block.value))
        return block.with_updates(value=decoded, metadata=deepcopy(block.metadata))

    def _can_read_link_endpoints(self, link: MemoryLink) -> bool:
        for block_id in (link.source_id, link.target_id):
            block = self.backend.get_block(block_id)
            if block is None or not self.policy_engine.allows(Operation.READ, block, self.principal):
                return False
        return True


@overload
def _block_id(block: str | MemoryBlock) -> str: ...


@overload
def _block_id(block: None) -> None: ...


def _block_id(block: str | MemoryBlock | None) -> str | None:
    if isinstance(block, MemoryBlock):
        return block.id
    return block
