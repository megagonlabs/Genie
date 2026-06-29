from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, overload

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.policy.engine import AccessDenied
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

if TYPE_CHECKING:
    from genie.memory.memory import Memory


class MemoryScope:
    """Scoped memory handle intended to be passed into an agent."""

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
        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().get(actual_id)

    def set(self, block_id: str | MemoryBlock, value: Any, **kwargs: Any) -> MemoryBlock:
        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().set(actual_id, value, **kwargs)

    def patch(self, block_id: str | MemoryBlock, **kwargs: Any) -> MemoryBlock:
        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().patch(actual_id, **kwargs)

    def delete(self, block_id: str | MemoryBlock) -> None:
        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().delete(actual_id)

    def children(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            raise AccessDenied("A rootless memory scope cannot infer which children to fetch.")
        self._require_in_subtree(actual_id)
        return self._as_principal().children(actual_id)

    def ancestors(self, block_id: str | MemoryBlock | None = None) -> list[MemoryBlock]:
        if not self.allow_up:
            raise AccessDenied("This memory scope does not allow ancestor traversal.")
        actual_id = _block_id(block_id) if block_id is not None else self.root_id
        if actual_id is None:
            raise AccessDenied("A rootless memory scope cannot infer which ancestors to fetch.")
        self._require_in_subtree(actual_id)
        return self._as_principal().ancestors(actual_id)

    def link(
        self,
        source: str | MemoryBlock,
        target: str | MemoryBlock,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        self._require_in_subtree(_block_id(source))
        self._require_in_subtree(_block_id(target))
        return self._as_principal().link(source, target, relation, metadata=metadata)

    def links(
        self,
        block_id: str | MemoryBlock,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        actual_id = _block_id(block_id)
        self._require_in_subtree(actual_id)
        return self._as_principal().links(actual_id, relation=relation, direction=direction)

    def search(
        self,
        query: MemoryQuery | str | None = None,
        *,
        scope: Scope | str | None = None,
        root: str | MemoryBlock | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        selected_scope = Scope(scope or self.default_scope)
        root_id = _block_id(root) if root is not None else self.root_id
        if selected_scope == Scope.GLOBAL and not self.allow_global:
            raise AccessDenied("This memory scope does not allow global search.")
        if selected_scope == Scope.ANCESTORS and not self.allow_up:
            raise AccessDenied("This memory scope does not allow ancestor search.")
        if root_id is not None and selected_scope not in {Scope.GLOBAL, Scope.ANCESTORS}:
            self._require_in_subtree(root_id)
        return self._as_principal().search(
            query,
            root=root_id,
            scope=selected_scope,
            **kwargs,
        )

    def scope(
        self,
        *,
        root: str | MemoryBlock | None = None,
        **kwargs: Any,
    ) -> "MemoryScope":
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
        return self._memory.__class__(
            backend=self._memory.backend,
            principal=self.principal,
            policy_engine=self._memory.policy_engine,
            hooks=self._memory.hooks,
            codecs=self._memory.codecs,
        )

    def _require_in_subtree(self, block_id: str | None) -> None:
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
    if isinstance(block, MemoryBlock):
        return block.id
    return block
