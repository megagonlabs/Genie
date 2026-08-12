"""Hook dispatch, causal context, suppression, and reentrancy guards."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from genie.hooks.base import MemoryHook
from genie.hooks.events import HookContext, HookOperation, StructureChange
from genie.memory.block import MemoryBlock
from genie.memory.errors import HookRecursionError
from genie.memory.links import MemoryLink
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


_CURRENT_OPERATION: ContextVar[HookContext | None] = ContextVar(
    "genie_current_hook_operation",
    default=None,
)
_ACTIVE_CALLBACKS: ContextVar[frozenset[tuple[int, str]]] = ContextVar(
    "genie_active_hook_callbacks",
    default=frozenset(),
)
_SUPPRESSED_REGISTRIES: ContextVar[frozenset[int]] = ContextVar(
    "genie_suppressed_hook_registries",
    default=frozenset(),
)
_SUPPRESSED_HOOKS: ContextVar[frozenset[tuple[int, int]]] = ContextVar(
    "genie_suppressed_hooks",
    default=frozenset(),
)
_SKIPPED = object()


class HookRegistry:
    """Ordered collection of hooks with bounded, reentrancy-safe dispatch.

    Dispatch methods require the explicit context yielded by ``operation``.
    ``Memory`` and structure facades manage this lifecycle for normal use.
    """

    def __init__(
        self,
        hooks: list[MemoryHook] | None = None,
        *,
        max_depth: int = 16,
    ) -> None:
        """Create a registry.

        Args:
            hooks: Initial hooks in dispatch order.
            max_depth: Maximum zero-based depth for operations started inside
                hooks. Top-level operations have depth zero.

        Raises:
            TypeError: If ``max_depth`` is not an integer.
            ValueError: If ``max_depth`` is negative.
        """

        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise TypeError("max_depth must be an integer.")
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative.")
        self._hooks = list(hooks or ())
        self.max_depth = max_depth

    def register(self, hook: MemoryHook) -> None:
        """Append a hook to the dispatch order.

        Args:
            hook: Hook to register.
        """

        self._hooks.append(hook)

    @contextmanager
    def operation(
        self,
        operation: HookOperation | str,
        *,
        principal_id: str | None = None,
    ) -> Iterator[HookContext]:
        """Create causal context around one logical operation.

        Args:
            operation: Logical operation being performed.
            principal_id: Acting principal when available.

        Yields:
            Context shared by the operation's before and after callbacks.

        Raises:
            HookRecursionError: If nested operations exceed ``max_depth``.
            ValueError: If ``operation`` is not a recognized hook operation.
        """

        if not isinstance(operation, HookOperation):
            operation = HookOperation(operation)
        parent = _CURRENT_OPERATION.get()
        depth = parent.depth + 1 if parent is not None else 0
        origin = "hook" if _ACTIVE_CALLBACKS.get() else "caller"
        if origin == "hook" and depth > self.max_depth:
            raise HookRecursionError(
                f"Hook operation depth {depth} exceeds configured maximum "
                f"{self.max_depth}."
            )
        context = HookContext(
            operation_id=f"hookop_{uuid4().hex}",
            parent_operation_id=parent.operation_id if parent is not None else None,
            operation=operation,
            origin=origin,
            depth=depth,
            principal_id=principal_id,
        )
        token = _CURRENT_OPERATION.set(context)
        try:
            yield context
        finally:
            _CURRENT_OPERATION.reset(token)

    @contextmanager
    def suppress(self, *hooks: MemoryHook) -> Iterator[None]:
        """Temporarily suppress this registry or selected hooks.

        This trusted escape hatch is intended for migrations and carefully
        controlled hook-internal writes. Normal recursive callbacks are
        handled automatically and do not require suppression.

        Args:
            *hooks: Specific hooks to suppress. Omit to suppress the complete
                registry in the current context.

        Yields:
            ``None`` while suppression is active.
        """

        if hooks:
            current = _SUPPRESSED_HOOKS.get()
            selected = frozenset((id(self), id(hook)) for hook in hooks)
            token = _SUPPRESSED_HOOKS.set(current | selected)
            try:
                yield
            finally:
                _SUPPRESSED_HOOKS.reset(token)
            return

        token = _SUPPRESSED_REGISTRIES.set(_SUPPRESSED_REGISTRIES.get() | {id(self)})
        try:
            yield
        finally:
            _SUPPRESSED_REGISTRIES.reset(token)

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Dispatch ``before_put`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_put", block)
            if result is not _SKIPPED:
                block = _require_type(result, MemoryBlock, "before_put")
        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch ``after_put`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_put", block)

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Dispatch ``before_update`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_update", current, proposed)
            if result is not _SKIPPED:
                proposed = _require_type(result, MemoryBlock, "before_update")
        return proposed

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Dispatch ``after_update`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_update", previous, stored)

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Dispatch ``before_get`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_get", block_id)

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Dispatch ``after_get`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_get", block)

    def before_search(
        self,
        context: HookContext,
        query: MemoryQuery,
    ) -> MemoryQuery:
        """Dispatch ``before_search`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_search", query)
            if result is not _SKIPPED:
                query = _require_type(result, MemoryQuery, "before_search")
        return query

    def after_search(
        self,
        context: HookContext,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Dispatch ``after_search`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "after_search", query, results)
            if result is not _SKIPPED:
                if not isinstance(result, list):
                    raise TypeError("after_search hooks must return a list.")
                results = result
        return results

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Dispatch ``before_add_link`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_add_link", link)
            if result is not _SKIPPED:
                link = _require_type(result, MemoryLink, "before_add_link")
        return link

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Dispatch ``after_add_link`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_add_link", link)

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Dispatch ``before_update_link`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_update_link", current, proposed)
            if result is not _SKIPPED:
                proposed = _require_type(result, MemoryLink, "before_update_link")
        return proposed

    def after_update_link(
        self,
        context: HookContext,
        previous: MemoryLink,
        stored: MemoryLink,
    ) -> None:
        """Dispatch ``after_update_link`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_update_link", previous, stored)

    def before_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Dispatch ``before_structure_change`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_structure_change", change)

    def after_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Dispatch ``after_structure_change`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_structure_change", change)

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch ``before_delete`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_delete", block)

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch ``after_delete`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "after_delete", block)

    def _hooks_for(self, context: HookContext) -> list[MemoryHook]:
        """Return registered hooks after validating the dispatch context."""

        if _CURRENT_OPERATION.get() is not context:
            raise ValueError(
                "Hook callbacks require the context yielded by the active "
                "HookRegistry.operation()."
            )
        return self._hooks

    def _invoke(
        self,
        context: HookContext,
        hook: MemoryHook,
        callback_name: str,
        *args: object,
    ) -> object:
        """Invoke one callback unless it is suppressed or already active."""

        if id(self) in _SUPPRESSED_REGISTRIES.get():
            return _SKIPPED
        if (id(self), id(hook)) in _SUPPRESSED_HOOKS.get():
            return _SKIPPED
        callback_key = (id(hook), callback_name)
        active = _ACTIVE_CALLBACKS.get()
        if callback_key in active:
            return _SKIPPED
        token = _ACTIVE_CALLBACKS.set(active | {callback_key})
        try:
            return getattr(hook, callback_name)(context, *args)
        finally:
            _ACTIVE_CALLBACKS.reset(token)


def _require_type(value: object, expected: type[Any], callback: str) -> Any:
    """Return a callback value after checking its public contract."""

    if not isinstance(value, expected):
        raise TypeError(f"{callback} hooks must return {expected.__name__}.")
    return value
