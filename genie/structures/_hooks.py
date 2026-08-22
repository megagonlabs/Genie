"""Internal hook integration shared by structure facades."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from genie.hooks import HookContext, HookOperation, HookRegistry


def resolve_hooks(access: object, explicit: HookRegistry | None) -> HookRegistry | None:
    """Resolve an explicit or memory-owned registry without widening handles.

    Args:
        access: Memory or handle access object a facade was built on.
        explicit: Registry supplied by the caller, if any.

    Returns:
        The explicit registry, the one the access object exposes, or `None`
        when neither is available.
    """

    if explicit is not None:
        return explicit
    candidate = getattr(access, "hooks", None)
    if isinstance(candidate, HookRegistry):
        return candidate
    memory = getattr(access, "_memory", None)
    candidate = getattr(memory, "hooks", None)
    return candidate if isinstance(candidate, HookRegistry) else None


def principal_id(access: object) -> str | None:
    """Return an access object's principal id when available.

    Args:
        access: Memory or handle access object a facade was built on.

    Returns:
        Principal id, or `None` when the access object exposes none.
    """

    principal = getattr(access, "principal", None)
    identifier = getattr(principal, "id", None)
    return identifier if isinstance(identifier, str) else None


def context_attributes(access: object) -> Mapping[str, str] | None:
    """Return an access object's hook context attributes when available.

    Args:
        access: Memory or handle access object a facade was built on.

    Returns:
        Hook context attributes, or `None` when the access object exposes
        none.
    """

    candidate = getattr(access, "attributes", None)
    return candidate if isinstance(candidate, Mapping) else None


def hook_operation(
    hooks: HookRegistry | None,
    operation: HookOperation,
    *,
    principal: str | None,
    attributes: Mapping[str, str] | None = None,
) -> AbstractContextManager[HookContext | None]:
    """Return a registry operation context or a no-op context.

    Args:
        hooks: Registry to open an operation on, or `None`.
        operation: Logical operation being performed.
        principal: Principal id to record on the context.
        attributes: Optional hook context attributes.

    Returns:
        A registry operation context manager, or one yielding `None` when no
        registry is configured.
    """

    if hooks is None:
        return nullcontext(None)
    return hooks.operation(operation, principal_id=principal, attributes=attributes)


def before_change(
    hooks: HookRegistry | None,
    context: HookContext | None,
    change: Any,
) -> None:
    """Dispatch a structural before callback when hooks are configured.

    Args:
        hooks: Registry to dispatch through, or `None` to do nothing.
        context: Active operation context.
        change: Structure change about to be applied.

    Raises:
        RuntimeError: If a registry is configured without an operation
            context.
    """

    if hooks is not None:
        if context is None:
            raise RuntimeError("A configured hook registry requires an operation context.")
        hooks.before_structure_change(context, change)


def after_change(
    hooks: HookRegistry | None,
    context: HookContext | None,
    change: Any,
) -> None:
    """Dispatch a structural after callback when hooks are configured.

    Args:
        hooks: Registry to dispatch through, or `None` to do nothing.
        context: Active operation context.
        change: Structure change that was applied.

    Raises:
        RuntimeError: If a registry is configured without an operation
            context.
    """

    if hooks is not None:
        if context is None:
            raise RuntimeError("A configured hook registry requires an operation context.")
        hooks.after_structure_change(context, change)
