"""Hook dispatch, causal context, suppression, and reentrancy guards."""

from __future__ import annotations

import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from genie.hooks.base import MemoryHook
from genie.hooks.events import (
    HandleCapability,
    HookContext,
    HookOperation,
    HookOrigin,
    HookOutcome,
    StructureChange,
    Traversal,
    freeze_attributes,
)
from genie.memory.block import MemoryBlock
from genie.memory.errors import HookRecursionError
from genie.memory.links import MemoryLink
from genie.policy.engine import AccessDenied, Operation
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
_LOGGER = logging.getLogger(__name__)


class HookRegistry:
    """Ordered collection of hooks with bounded, reentrancy-safe dispatch.

    Dispatch methods require the explicit context yielded by ``operation``.
    ``Memory`` and structure facades manage this lifecycle for normal use.

    Observing callbacks are isolated from one another. Every hook receives
    ``after_put``, ``after_update``, ``after_get``, ``after_traverse``,
    ``after_add_link``, ``after_update_link``, ``after_structure_change``,
    ``after_delete``, ``on_denied``, and ``on_operation_end`` even when another
    hook raises, and a recursive delete reports every removed block before any
    failure surfaces. What these callbacks report is already decided, so
    stopping at the first failure would only destroy the records the remaining
    hooks and blocks are owed. Collected failures are still reported: they are
    attached as notes to an exception the caller is already receiving, raised
    when there is none, and merely logged for hooks that declare ``fail_open``.

    Callbacks that can still change what the caller stores or receives, meaning
    every ``before_`` callback and ``after_search``, fail fast and ignore
    ``fail_open``.
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
        attributes: Mapping[str, str] | None = None,
    ) -> Generator[HookContext, None, None]:
        """Create causal context around one logical operation.

        Leaving the context dispatches ``on_operation_end`` exactly once for
        every operation that starts, so hooks always observe a terminal event
        even when the operation is refused or fails.

        Args:
            operation: Logical operation being performed. Use ``scope`` for
                spans Genie does not model.
            principal_id: Acting principal when available. Defaults to the
                principal of the enclosing operation or scope.
            attributes: Attributes to add to the ones inherited from the
                enclosing operation or scope. Added attributes win on conflict.

        Yields:
            Context shared by the operation's before and after callbacks.

        Raises:
            HookRecursionError: If nested operations exceed ``max_depth``. The
                operation does not start, so no terminal event is dispatched.
            TypeError: If the attributes are not strings.
            ValueError: If ``operation`` is not a recognized hook operation, or
                is ``HookOperation.SCOPE``, which ``scope`` opens instead.
        """

        if not isinstance(operation, HookOperation):
            operation = HookOperation(operation)
        if operation is HookOperation.SCOPE:
            raise ValueError(
                "HookOperation.SCOPE names a caller-defined span. Open it with "
                "HookRegistry.scope() so no callback is dispatched for it."
            )
        context = self._new_context(operation, principal_id, attributes)
        token = _CURRENT_OPERATION.set(context)
        outcome = HookOutcome.SUCCESS
        error: BaseException | None = None
        try:
            yield context
        except BaseException as raised:
            error = raised
            outcome = HookOutcome.DENIED if isinstance(raised, AccessDenied) else HookOutcome.ERROR
            raise
        finally:
            try:
                self._dispatch_operation_end(context, outcome, error)
            finally:
                _CURRENT_OPERATION.reset(token)

    @contextmanager
    def scope(
        self,
        name: str,
        *,
        principal_id: str | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> Generator[HookContext, None, None]:
        """Group the memory operations of one caller-defined logical span.

        A scope names work Genie does not model, such as an agent run, a turn,
        or a tool call. It opens causal context without dispatching any
        callback: operations started inside it become children of the scope and
        inherit its attributes at any nesting depth. Hooks therefore receive
        the grouping key on every event without the scope itself appearing as
        an operation.

        Args:
            name: Caller-defined span name, such as ``"agent.turn"``. Scope
                names are free-form; the context reports it as ``scope_name``
                under the ``HookOperation.SCOPE`` operation.
            principal_id: Acting principal when available. Defaults to the
                principal of the enclosing operation or scope.
            attributes: Attributes inherited by everything inside the scope,
                added to the ones inherited from an enclosing operation or
                scope.

        Yields:
            Context describing the scope. Hooks never receive it directly; it
            reaches them as the parent operation id and inherited attributes of
            the operations inside.

        Raises:
            HookRecursionError: If scopes opened inside hooks exceed
                ``max_depth``.
            TypeError: If ``name`` is not a string or the attributes are not
                strings.
            ValueError: If ``name`` is empty.
        """

        if not isinstance(name, str):
            raise TypeError("Scope name must be a string.")
        if not name.strip():
            raise ValueError("Scope name must not be empty.")
        context = self._new_context(
            HookOperation.SCOPE,
            principal_id,
            attributes,
            scope_name=name,
        )
        token = _CURRENT_OPERATION.set(context)
        try:
            yield context
        finally:
            _CURRENT_OPERATION.reset(token)

    @contextmanager
    def suppress(self, *hooks: MemoryHook) -> Generator[None, None, None]:
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
            hooks_token = _SUPPRESSED_HOOKS.set(current | selected)
            try:
                yield
            finally:
                _SUPPRESSED_HOOKS.reset(hooks_token)
            return

        registries_token = _SUPPRESSED_REGISTRIES.set(
            _SUPPRESSED_REGISTRIES.get() | {id(self)}
        )
        try:
            yield
        finally:
            _SUPPRESSED_REGISTRIES.reset(registries_token)

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Dispatch ``before_put`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_put", block)
            if result is not _SKIPPED:
                block = _require_type(result, MemoryBlock, "before_put")
        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch ``after_put`` in registration order."""

        self._dispatch_observers(context, "after_put", block)

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

        self._dispatch_observers(context, "after_update", previous, stored)

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Dispatch ``before_get`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_get", block_id)

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Dispatch ``after_get`` in registration order."""

        self._dispatch_observers(context, "after_get", block)

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
        """Dispatch ``after_search`` in registration order.

        These hooks rerank and filter what the caller receives, so a failure
        stops dispatch instead of passing unreviewed results through.
        """

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "after_search", query, results)
            if result is not _SKIPPED:
                if not isinstance(result, list):
                    raise TypeError("after_search hooks must return a list.")
                if any(not isinstance(item, SearchResult) for item in result):
                    raise TypeError(
                        "after_search hooks must return only SearchResult values."
                    )
                results = result
        return results

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Dispatch ``before_traverse`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_traverse", traversal)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Dispatch ``after_traverse`` in registration order."""

        self._dispatch_observers(context, "after_traverse", traversal)

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Dispatch ``before_add_link`` in registration order."""

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_add_link", link)
            if result is not _SKIPPED:
                link = _require_type(result, MemoryLink, "before_add_link")
        return link

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Dispatch ``after_add_link`` in registration order."""

        self._dispatch_observers(context, "after_add_link", link)

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

        self._dispatch_observers(context, "after_update_link", previous, stored)

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

        self._dispatch_observers(context, "after_structure_change", change)

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch ``before_delete`` in registration order."""

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_delete", block)

    def after_delete(self, context: HookContext, *blocks: MemoryBlock) -> None:
        """Dispatch ``after_delete`` for every deleted block, in order.

        A recursive delete commits its whole subtree at once, so the record of
        one removed block is not the caller's to trade for another's: every
        block is reported to every hook before any failure surfaces.

        Args:
            context: Active operation context.
            *blocks: Deleted blocks, in deletion order.
        """

        failures: list[Exception] = []
        for block in blocks:
            failures.extend(self._collect(context, "after_delete", block))
        _report_failures(failures, "after_delete")

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Dispatch ``on_denied`` in registration order.

        A refusal must reach the caller unchanged, so every hook is called even
        when one raises. Failures are attached to ``error`` as notes when it is
        provided and raised only when the caller has no refusal to receive.

        Args:
            context: Active operation context.
            operation: Policy operation or handle capability that refused.
            subject_id: Block the refusal applies to, when one is known.
            reason: Message describing the refusal.
            error: Refusal being raised, so hook failures cannot replace it.
        """

        failures = self._collect(context, "on_denied", operation, subject_id, reason)
        _report_failures(failures, "on_denied", error)

    def _new_context(
        self,
        operation: HookOperation,
        principal_id: str | None,
        attributes: Mapping[str, str] | None,
        *,
        scope_name: str | None = None,
    ) -> HookContext:
        """Create context nested under the active operation or scope.

        Args:
            operation: Logical operation, or ``HookOperation.SCOPE``.
            principal_id: Acting principal, or `None` to inherit one.
            attributes: Attributes to add to the inherited ones.
            scope_name: Caller-defined span name for a scope.

        Returns:
            Context describing the new operation or scope.

        Raises:
            HookRecursionError: If a hook-originated context exceeds
                ``max_depth``.
            TypeError: If the attributes are not strings.
        """

        parent = _CURRENT_OPERATION.get()
        depth = parent.depth + 1 if parent is not None else 0
        origin: HookOrigin = "hook" if _ACTIVE_CALLBACKS.get() else "caller"
        if origin == "hook" and depth > self.max_depth:
            raise HookRecursionError(
                f"Hook operation depth {depth} exceeds configured maximum "
                f"{self.max_depth}."
            )
        added = freeze_attributes(attributes)
        acting = principal_id
        if acting is None and parent is not None:
            acting = parent.principal_id
        return HookContext(
            operation_id=f"hookop_{uuid4().hex}",
            parent_operation_id=parent.operation_id if parent is not None else None,
            operation=operation,
            origin=origin,
            depth=depth,
            principal_id=acting,
            attributes=added if parent is None else {**parent.attributes, **added},
            scope_name=scope_name,
        )

    def _dispatch_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Dispatch the terminal callback once per hook in registration order.

        Every hook is called even when an earlier hook raises, so one
        unreliable observer cannot cost another hook the cleanup or audit
        record it is owed. Failures are reported as notes on an operation that
        already failed, and raised when the operation itself succeeded.
        """

        failures = self._collect(context, "on_operation_end", outcome, error)
        _report_failures(failures, "on_operation_end", error)

    def _hooks_for(self, context: HookContext) -> list[MemoryHook]:
        """Return registered hooks after validating the dispatch context."""

        if _CURRENT_OPERATION.get() is not context:
            raise ValueError(
                "Hook callbacks require the context yielded by the active "
                "HookRegistry.operation()."
            )
        if context.is_scope:
            raise ValueError(
                "Hook callbacks cannot be dispatched with a scope context. A "
                "scope groups the operations inside it and dispatches nothing."
            )
        return self._hooks

    def _dispatch_observers(
        self,
        context: HookContext,
        callback_name: str,
        *args: object,
    ) -> None:
        """Dispatch one observing callback to every hook, then report failures.

        Args:
            context: Active operation context.
            callback_name: Observing callback to invoke on each hook.
            *args: Arguments passed after the context.

        Raises:
            Exception: The first collected failure, once every hook has run.
        """

        _report_failures(self._collect(context, callback_name, *args), callback_name)

    def _collect(
        self,
        context: HookContext,
        callback_name: str,
        *args: object,
    ) -> list[Exception]:
        """Invoke one observing callback on every hook and collect its failures.

        Dispatch continues past a raising hook because the work being observed
        is already decided: the failure cannot undo it, and stopping would only
        deny the remaining hooks their record. Failures from hooks that declare
        ``fail_open`` are logged and dropped here rather than returned.

        Args:
            context: Active operation context.
            callback_name: Observing callback to invoke on each hook.
            *args: Arguments passed after the context.

        Returns:
            Failures raised by hooks that are not fail-open, in dispatch order.
        """

        failures: list[Exception] = []
        for hook in self._hooks_for(context):
            try:
                self._invoke(context, hook, callback_name, *args)
            except Exception as raised:
                if _fails_open(hook):
                    _LOGGER.error(
                        "Contained %s failure from fail-open hook %s during %s.",
                        callback_name,
                        type(hook).__name__,
                        context.operation,
                        exc_info=raised,
                    )
                    continue
                failures.append(raised)
        return failures

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


def _fails_open(hook: MemoryHook) -> bool:
    """Return whether a hook's observing failures must stay contained."""

    return bool(getattr(hook, "fail_open", False))


def _report_failures(
    failures: list[Exception],
    callback: str,
    error: BaseException | None = None,
) -> None:
    """Report collected callback failures without losing or substituting any.

    Args:
        failures: Failures collected from hooks that are not fail-open.
        callback: Callback that failed, named in the reported notes.
        error: Exception already leaving the operation, when there is one. Hook
            failures become notes on it so they cannot replace what the caller
            is receiving.

    Raises:
        Exception: The first collected failure, when the operation has no error
            of its own to preserve. Any remaining failures are attached to it
            as notes so no failure is hidden by another.
    """

    if not failures:
        return
    if error is not None:
        for failure in failures:
            error.add_note(f"{callback} hook failed: {failure!r}")
        return
    first, *rest = failures
    for failure in rest:
        first.add_note(f"Another {callback} hook failed: {failure!r}")
    raise first


def _require_type(value: object, expected: type[Any], callback: str) -> Any:
    """Return a callback value after checking its public contract."""

    if not isinstance(value, expected):
        raise TypeError(f"{callback} hooks must return {expected.__name__}.")
    return value
