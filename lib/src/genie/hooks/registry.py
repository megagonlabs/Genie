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
    RelationProtection,
    StructureChange,
    Suppression,
    SuppressionScope,
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
    """Ordered collection of hooks, with a depth limit and a guard against reentry.

    Dispatch methods accept only the context that `operation` yields. `Memory`
    and the structure facades manage this lifecycle for normal use.

    Isolated callbacks:
        - Each hook gets `after_put`, `after_update`, `after_get`,
          `after_traverse`, `after_add_link`, `after_update_link`,
          `after_protect_relation`, `after_structure_change`, `after_delete`,
          `on_denied`, and `on_operation_end`, also when another hook raises.
          A recursive delete reports each removed block before any failure
          surfaces.
        - These callbacks report work that is already decided. A stop at the
          first failure only takes the records away from the other hooks and
          blocks.
        - The registry still reports collected failures. It attaches them as
          notes to an exception that the caller already gets, and logs them,
          because code can catch that exception and drop it. If there is no
          such exception, it raises them. For hooks that declare `fail_open`,
          it only logs them.
        - The registry collects only `Exception` failures. A `BaseException`,
          such as `KeyboardInterrupt`, stops dispatch at once.

    Fail-fast callbacks:
        - Each `before_` callback and `after_search` can still change what the
          caller stores or gets. So they fail fast and ignore `fail_open`.

    Auditable membership:
        - Registration uses object identity and rejects a repeat. So the
          registry cannot dispatch a hook twice and report one operation as two.
        - `hooks` lists the registered hooks, so startup code can assert that an
          audit hook is present.
        - A hook registered as mandatory cannot be unregistered or suppressed.

    Suppression:
        - Each suppression is an operation.
        - `on_suppressed` is isolated like the other observing callbacks. But it
          reports work that has not happened yet. So a failure there refuses the
          window, and the registry does not report the failure after the window.
    """

    def __init__(
        self,
        hooks: list[MemoryHook] | None = None,
        *,
        max_depth: int = 16,
    ) -> None:
        """Create a registry.

        Args:
            hooks: Initial hooks in dispatch order. For a mandatory hook, use
                `register`, which accepts the flag.
            max_depth: Maximum zero-based depth for operations that start
                inside hooks. Top-level operations have depth zero.

        Raises:
            TypeError: If `max_depth` is not an integer or an initial hook is
                not a `MemoryHook`.
            ValueError: If `max_depth` is negative or a hook appears twice.
        """

        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise TypeError("max_depth must be an integer.")
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative.")
        self._hooks: list[MemoryHook] = []
        self._mandatory: set[int] = set()
        self.max_depth = max_depth
        for hook in hooks or ():
            self.register(hook)

    @property
    def hooks(self) -> tuple[MemoryHook, ...]:
        """Registered hooks in dispatch order.

        The tuple is a snapshot. A later `register` or `unregister` call does
        not change it, and a hook cannot change the dispatch order through it.
        """

        return tuple(self._hooks)

    @property
    def mandatory(self) -> tuple[MemoryHook, ...]:
        """Registered mandatory hooks in dispatch order.

        These hooks continue to report through each suppression window, and
        they cannot be unregistered. So a startup check asserts this set when
        an audit trail has to stay in place for all code that runs later.
        """

        return tuple(hook for hook in self._hooks if id(hook) in self._mandatory)

    def register(self, hook: MemoryHook, *, mandatory: bool = False) -> None:
        """Add a hook at the end of the dispatch order.

        - **Identity:** the registry identifies hooks by object identity. So 2
          instances of the same class are 2 hooks.
        - **No repeat:** a second registration of the same instance raises
          `ValueError`. Without this check, the hook reports each operation
          twice.

        Args:
            hook: Hook to register.
            mandatory: Whether the hook is mandatory. A mandatory hook cannot
                be unregistered or suppressed. So it is the way to install an
                audit hook that the rest of the process cannot detach. The flag
                applies only on the first registration, because a second
                `register` call for the same hook raises `ValueError`.

        Raises:
            TypeError: If `hook` is not a `MemoryHook`.
            ValueError: If the hook is already registered.
        """

        if not isinstance(hook, MemoryHook):
            raise TypeError("hook must be a MemoryHook.")
        if self._index_of(hook) is not None:
            raise ValueError(
                f"{type(hook).__name__} is already registered. Registering it "
                "again would report every operation twice."
            )
        self._hooks.append(hook)
        if mandatory:
            self._mandatory.add(id(hook))

    def unregister(self, hook: MemoryHook) -> None:
        """Remove a hook from the dispatch order.

        A hook that is registered again later goes to the end. So a removal
        and a new registration change the dispatch order.

        Args:
            hook: Hook to remove.

        Raises:
            ValueError: If the hook is not registered or is mandatory.
        """

        index = self._index_of(hook)
        if index is None:
            raise ValueError(f"{type(hook).__name__} is not registered.")
        if id(hook) in self._mandatory:
            raise ValueError(
                f"{type(hook).__name__} was registered as mandatory and cannot "
                "be unregistered."
            )
        del self._hooks[index]

    @contextmanager
    def operation(
        self,
        operation: HookOperation | str,
        *,
        principal_id: str | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> Generator[HookContext, None, None]:
        """Open the causal context of one logical operation.

        When the context closes, the registry dispatches `on_operation_end`
        exactly once for each operation that starts. So hooks always get a
        terminal event, also when the operation is refused or fails.

        Args:
            operation: Logical operation. `scope` opens spans that Genie does
                not model.
            principal_id: Acting principal, if available. Defaults to the
                principal of the operation or scope that contains this one.
            attributes: Attributes to add to the attributes inherited from the
                operation or scope that contains this one. Added attributes win
                on conflict.

        Yields:
            Context that the before and after callbacks of the operation share.

        Raises:
            HookRecursionError: If nested operations go above `max_depth`. The
                operation does not start, so the registry dispatches no
                terminal event.
            TypeError: If the attributes are not a mapping of strings to
                strings.
            ValueError: If `operation` is not a known hook operation, or is
                `HookOperation.SCOPE` or `HookOperation.SUPPRESS`. `scope` and
                `suppress` open those operations.
        """

        if not isinstance(operation, HookOperation):
            operation = HookOperation(operation)
        if operation is HookOperation.SCOPE:
            raise ValueError(
                "HookOperation.SCOPE names a caller-defined span. Open it with "
                "HookRegistry.scope() so no callback is dispatched for it."
            )
        if operation is HookOperation.SUPPRESS:
            raise ValueError(
                "HookOperation.SUPPRESS reports disabled dispatch. Open it with "
                "HookRegistry.suppress() so the window is announced to hooks."
            )
        with self._operation(operation, principal_id, attributes) as context:
            yield context

    @contextmanager
    def _operation(
        self,
        operation: HookOperation,
        principal_id: str | None,
        attributes: Mapping[str, str] | None,
    ) -> Generator[HookContext, None, None]:
        """Open the causal context, and dispatch the terminal event when the context closes."""

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

        - **Purpose:** a scope names work that Genie does not model, such as an
          agent run, a turn, or a tool call.
        - **No callbacks:** it opens causal context and dispatches no callback.
          Operations that start inside it become children of the scope, and
          they inherit its attributes at any depth.
        - **Result:** hooks get the group key on each event, but the scope
          itself does not appear as an operation.

        Args:
            name: Caller-defined span name, such as `"agent.turn"`. The name is
                free-form. The context reports it as `scope_name` under the
                `HookOperation.SCOPE` operation.
            principal_id: Acting principal, if available. Defaults to the
                principal of the operation or scope that contains this one.
            attributes: Attributes that everything inside the scope inherits.
                The registry adds them to the attributes inherited from an
                operation or scope that contains this one.

        Yields:
            Context of the scope. Hooks never get it directly. It gets to them
            as the parent operation id and the inherited attributes of the
            operations inside.

        Raises:
            HookRecursionError: If scopes opened inside hooks go above
                `max_depth`.
            TypeError: If `name` is not a string, or the attributes are not a
                mapping of strings to strings.
            ValueError: If `name` is empty or contains only whitespace.
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
    def suppress(
        self,
        *hooks: MemoryHook,
        principal_id: str | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> Generator[HookContext, None, None]:
        """Suppress this registry or selected hooks for a short time.

        - **Use:** this trusted escape hatch is for migrations and for writes
          inside hooks that are under careful control. The registry already
          handles normal recursive callbacks, so they do not need suppression.
        - **An operation, not a silent gap:** a trail cannot lose the record
          that hooks are off. So the window is an operation:
          - Each hook gets `on_suppressed` before dispatch stops. This includes
            the hooks that the window suppresses.
          - The operations inside the window report it as their parent.
          - Each hook gets the terminal event of the window after dispatch
            starts again.
          - A mandatory hook continues to report during the window. So it
            records the window and its contents, not only its start and end.
        - **Nested windows:** a hook that an outer window silences gets no
          events from an inner window.

        Args:
            *hooks: Hooks to suppress. With no hooks, the call suppresses the
                complete registry in the current context. This includes hooks
                that are registered while the window is open. Mandatory hooks
                are never suppressed.
            principal_id: Principal that opens the window. Defaults to the
                principal of the operation or scope that contains it.
            attributes: Attributes to add to the inherited attributes, such as
                the migration or ticket that the window is for. Operations
                inside the window inherit them.

        Yields:
            Context of the window. Its terminal event shares it, and the
            operations inside the window inherit it.

        Raises:
            Exception: The first failure that `on_suppressed` raises, after
                each hook got the callback. Dispatch stays on, and the
                suppressed body does not run. This callback exists to prevent
                a suppression that no hook can record.
            HookRecursionError: If windows opened inside hooks go above
                `max_depth`.
            TypeError: If the attributes are not a mapping of strings to
                strings.
            ValueError: If a named hook is not registered or is mandatory. The
                window does not open.
        """

        suppression = self._plan_suppression(hooks)
        with self._operation(HookOperation.SUPPRESS, principal_id, attributes) as context:
            _report_failures(
                self._collect(context, "on_suppressed", suppression),
                "on_suppressed",
            )
            with self._suppressed(suppression):
                yield context

    @contextmanager
    def _suppressed(self, suppression: Suppression) -> Generator[None, None, None]:
        """Turn off dispatch for the hooks of the suppression, in the current context only."""

        if suppression.scope is SuppressionScope.REGISTRY:
            registries_token = _SUPPRESSED_REGISTRIES.set(
                _SUPPRESSED_REGISTRIES.get() | {id(self)}
            )
            try:
                yield
            finally:
                _SUPPRESSED_REGISTRIES.reset(registries_token)
            return

        selected = frozenset((id(self), id(hook)) for hook in suppression.hooks)
        hooks_token = _SUPPRESSED_HOOKS.set(_SUPPRESSED_HOOKS.get() | selected)
        try:
            yield
        finally:
            _SUPPRESSED_HOOKS.reset(hooks_token)

    def _plan_suppression(self, hooks: tuple[MemoryHook, ...]) -> Suppression:
        """Describe the window that a `suppress` call opens.

        Args:
            hooks: Hooks that the caller names, or empty for the whole registry.

        Returns:
            The window. It names the hooks that no longer report, in dispatch
            order, and the mandatory hooks that continue to report.

        Raises:
            ValueError: If a named hook is not registered or is mandatory.
        """

        if not hooks:
            return Suppression(
                scope=SuppressionScope.REGISTRY,
                hooks=tuple(
                    hook for hook in self._hooks if id(hook) not in self._mandatory
                ),
                mandatory=self.mandatory,
            )
        for hook in hooks:
            if self._index_of(hook) is None:
                raise ValueError(
                    f"{type(hook).__name__} is not registered by this registry, "
                    "so suppressing it would silence nothing."
                )
            if id(hook) in self._mandatory:
                raise ValueError(
                    f"{type(hook).__name__} was registered as mandatory and "
                    "cannot be suppressed."
                )
        return Suppression(
            scope=SuppressionScope.SELECTED,
            hooks=tuple(
                hook
                for hook in self._hooks
                if any(hook is selected for selected in hooks)
            ),
            mandatory=self.mandatory,
        )

    def _index_of(self, hook: MemoryHook) -> int | None:
        """Return the position of a hook in the dispatch order, by identity, or `None`."""

        for index, registered in enumerate(self._hooks):
            if registered is hook:
                return index
        return None

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Dispatch `before_put` in registration order.

        Args:
            context: Active operation context.
            block: Block before the backend stores it.

        Returns:
            Block from the last hook that changed it, or `block` if no hook
            changed it.

        Raises:
            TypeError: If a hook returns an object that is not a `MemoryBlock`.
        """

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_put", block)
            if result is not _SKIPPED:
                block = _require_type(result, MemoryBlock, "before_put")
        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch `after_put` in registration order.

        Args:
            context: Active operation context.
            block: Stored block.
        """

        self._dispatch_observers(context, "after_put", block)

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Dispatch `before_update` in registration order.

        Args:
            context: Active operation context.
            current: Block as it is stored now.
            proposed: Block that the caller wants to store.

        Returns:
            Replacement block from the last hook that changed it, or `proposed`
            if no hook changed it.

        Raises:
            TypeError: If a hook returns an object that is not a `MemoryBlock`.
        """

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
        """Dispatch `after_update` in registration order.

        Args:
            context: Active operation context.
            previous: Block as it was before the update.
            stored: Block as stored after the update.
        """

        self._dispatch_observers(context, "after_update", previous, stored)

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Dispatch `before_get` in registration order.

        Args:
            context: Active operation context.
            block_id: Id of the block to read.
        """

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_get", block_id)

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Dispatch `after_get` in registration order.

        Args:
            context: Active operation context.
            block: Block that was read, or `None` if the block does not
                exist.
        """

        self._dispatch_observers(context, "after_get", block)

    def before_search(
        self,
        context: HookContext,
        query: MemoryQuery,
    ) -> MemoryQuery:
        """Dispatch `before_search` in registration order.

        Args:
            context: Active operation context.
            query: Normalized query before it runs.

        Returns:
            Query from the last hook that changed it, or `query` if no hook
            changed it.

        Raises:
            TypeError: If a hook returns an object that is not a `MemoryQuery`.
        """

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
        """Dispatch `after_search` in registration order.

        These hooks can rerank and filter what the caller gets. So a failure
        stops dispatch, and results that a hook did not review do not get to
        the caller.

        Args:
            context: Active operation context.
            query: Query that gave the results.
            results: Results that the search collected.

        Returns:
            Results from the last hook that changed them, or `results` if no
            hook changed them.

        Raises:
            TypeError: If a hook returns an object that is not a list of
                `SearchResult` values.
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
        """Dispatch `before_traverse` in registration order.

        Args:
            context: Active operation context.
            traversal: Traversal before it runs.
        """

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_traverse", traversal)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Dispatch `after_traverse` in registration order.

        Args:
            context: Active operation context.
            traversal: Traversal that ran, with its results.
        """

        self._dispatch_observers(context, "after_traverse", traversal)

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Dispatch `before_add_link` in registration order.

        Args:
            context: Active operation context.
            link: Link before the backend stores it.

        Returns:
            Link from the last hook that changed it, or `link` if no hook
            changed it.

        Raises:
            TypeError: If a hook returns an object that is not a `MemoryLink`.
        """

        for hook in self._hooks_for(context):
            result = self._invoke(context, hook, "before_add_link", link)
            if result is not _SKIPPED:
                link = _require_type(result, MemoryLink, "before_add_link")
        return link

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Dispatch `after_add_link` in registration order.

        Args:
            context: Active operation context.
            link: Stored link.
        """

        self._dispatch_observers(context, "after_add_link", link)

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Dispatch `before_update_link` in registration order.

        Args:
            context: Active operation context.
            current: Link as it is stored now.
            proposed: Link that the caller wants to store.

        Returns:
            Replacement link from the last hook that changed it, or `proposed`
            if no hook changed it.

        Raises:
            TypeError: If a hook returns an object that is not a `MemoryLink`.
        """

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
        """Dispatch `after_update_link` in registration order.

        Args:
            context: Active operation context.
            previous: Link as it was before the update.
            stored: Link as stored after the update.
        """

        self._dispatch_observers(context, "after_update_link", previous, stored)

    def before_protect_relation(self, context: HookContext, protection: RelationProtection) -> None:
        """Dispatch `before_protect_relation` in registration order.

        Args:
            context: Active operation context.
            protection: Protection before the backend runs it.
        """

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_protect_relation", protection)

    def after_protect_relation(self, context: HookContext, protection: RelationProtection) -> None:
        """Dispatch `after_protect_relation` in registration order.

        Args:
            context: Active operation context.
            protection: Committed protection.
        """

        self._dispatch_observers(context, "after_protect_relation", protection)

    def before_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Dispatch `before_structure_change` in registration order.

        Args:
            context: Active operation context.
            change: Structure change before the backend applies it.
        """

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_structure_change", change)

    def after_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Dispatch `after_structure_change` in registration order.

        Args:
            context: Active operation context.
            change: Structure change that the backend applied.
        """

        self._dispatch_observers(context, "after_structure_change", change)

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Dispatch `before_delete` in registration order.

        Args:
            context: Active operation context.
            block: Block before the backend deletes it.
        """

        for hook in self._hooks_for(context):
            self._invoke(context, hook, "before_delete", block)

    def after_delete(self, context: HookContext, *blocks: MemoryBlock) -> None:
        """Dispatch `after_delete` for each deleted block, in order.

        A recursive delete commits its whole subtree at once. So a hook failure
        on one removed block cannot remove the record of another block. Each
        block gets to each hook before any failure surfaces.

        Args:
            context: Active operation context.
            *blocks: Deleted blocks, in the order of deletion.
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
        """Dispatch `on_denied` in registration order.

        The refusal gets to the caller with no change. So the registry calls
        each hook, also when one raises. If `error` has a value, the registry
        attaches the failures to it as notes and logs them. It raises them
        only when the caller gets no refusal.

        Args:
            context: Active operation context.
            operation: Policy operation or handle capability that refused.
            subject_id: Block that the refusal applies to, if known.
            reason: Message that describes the refusal.
            error: Refusal that the caller gets. The registry attaches hook
                failures to it, so they cannot replace it.
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
        """Create a context under the active operation or scope.

        Args:
            operation: Logical operation, or `HookOperation.SCOPE`.
            principal_id: Acting principal, or `None` to inherit one.
            attributes: Attributes to add to the inherited attributes.
            scope_name: Caller-defined span name for a scope.

        Returns:
            Context of the new operation or scope.

        Raises:
            HookRecursionError: If a context that starts inside a hook goes
                above `max_depth`.
            TypeError: If the attributes are not a mapping of strings to
                strings.
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
        """Dispatch the terminal callback once to each hook, in registration order.

        The registry calls each hook, also when an earlier hook raises. So one
        unreliable observer cannot make another hook lose its cleanup or its
        audit record. If the operation already failed, the failures become
        notes on its error. If the operation succeeded, the registry raises
        them.
        """

        failures = self._collect(context, "on_operation_end", outcome, error)
        _report_failures(failures, "on_operation_end", error)

    def _hooks_for(self, context: HookContext) -> tuple[MemoryHook, ...]:
        """Check the dispatch context, then return the registered hooks.

        The registry takes a snapshot of the dispatch order. So if a callback
        registers or unregisters a hook, the change applies from the next
        event. It does not skip a hook in the current event.

        Raises:
            ValueError: If `context` is not the active operation context, or is
                a scope context.
        """

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
        return tuple(self._hooks)

    def _dispatch_observers(
        self,
        context: HookContext,
        callback_name: str,
        *args: object,
    ) -> None:
        """Dispatch one observing callback to each hook, then report the failures.

        Args:
            context: Active operation context.
            callback_name: Observing callback to call on each hook.
            *args: Arguments that follow the context.

        Raises:
            Exception: The first collected failure, after all hooks ran.
        """

        _report_failures(self._collect(context, callback_name, *args), callback_name)

    def _collect(
        self,
        context: HookContext,
        callback_name: str,
        *args: object,
    ) -> list[Exception]:
        """Call one observing callback on each hook, and collect its failures.

        - **Dispatch continues after a hook raises.** The work that the callback
          reports is already decided. The failure cannot undo it, and a stop
          only takes the record away from the other hooks.
        - **`fail_open` hooks:** the registry logs their failures and drops
          them. It does not return them.
        - **`Exception` only:** the registry does not collect a
          `BaseException`, such as `KeyboardInterrupt`. It stops dispatch at
          once.

        Args:
            context: Active operation context.
            callback_name: Observing callback to call on each hook.
            *args: Arguments that follow the context.

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
        """Call one callback, unless it is suppressed or already active, and return `_SKIPPED` if it did not run."""

        if id(hook) not in self._mandatory:
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
    """Return whether a hook declares `fail_open`."""

    return bool(getattr(hook, "fail_open", False))


def _report_failures(
    failures: list[Exception],
    callback: str,
    error: BaseException | None = None,
) -> None:
    """Report collected callback failures, with no failure lost or replaced.

    - **Notes and a log:** a failure that becomes a note on `error` also goes
      to the log. Code between the registry and the caller can catch `error`
      and drop it. For example, a structure listing drops the refusal of each
      hidden member, because a raised error would show that the member exists.

    Args:
        failures: Failures collected from hooks that are not fail-open.
        callback: Callback that failed. The reported notes name it.
        error: Exception that already leaves the operation, if there is one.
            Hook failures become notes on it, so they cannot replace what the
            caller gets.

    Raises:
        Exception: The first collected failure, when the operation has no error
            of its own to keep. The other failures become notes on it, so no
            failure hides another.
    """

    if not failures:
        return
    if error is not None:
        for failure in failures:
            error.add_note(f"{callback} hook failed: {failure!r}")
            _LOGGER.error(
                "Attached %s failure to %s.",
                callback,
                type(error).__name__,
                exc_info=failure,
            )
        return
    first, *rest = failures
    for failure in rest:
        first.add_note(f"Another {callback} hook failed: {failure!r}")
    raise first


def _require_type(value: object, expected: type[Any], callback: str) -> Any:
    """Check that a callback value has the expected type, and return the value."""

    if not isinstance(value, expected):
        raise TypeError(f"{callback} hooks must return {expected.__name__}.")
    return value
