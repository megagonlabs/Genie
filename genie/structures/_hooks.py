"""Internal hook integration shared by structure facades."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from genie.hooks import HookContext, HookOperation, HookRegistry


def resolve_hooks(access: object, explicit: HookRegistry | None) -> HookRegistry | None:
    """Resolve an explicit or memory-owned registry without widening handles."""

    if explicit is not None:
        return explicit
    candidate = getattr(access, "hooks", None)
    if isinstance(candidate, HookRegistry):
        return candidate
    memory = getattr(access, "_memory", None)
    candidate = getattr(memory, "hooks", None)
    return candidate if isinstance(candidate, HookRegistry) else None


def principal_id(access: object) -> str | None:
    """Return an access object's principal id when available."""

    principal = getattr(access, "principal", None)
    identifier = getattr(principal, "id", None)
    return identifier if isinstance(identifier, str) else None


def context_attributes(access: object) -> Mapping[str, str] | None:
    """Return an access object's hook context attributes when available."""

    candidate = getattr(access, "attributes", None)
    return candidate if isinstance(candidate, Mapping) else None


def hook_operation(
    hooks: HookRegistry | None,
    operation: HookOperation,
    *,
    principal: str | None,
    attributes: Mapping[str, str] | None = None,
) -> AbstractContextManager[HookContext | None]:
    """Return a registry operation context or a no-op context."""

    if hooks is None:
        return nullcontext(None)
    return hooks.operation(operation, principal_id=principal, attributes=attributes)


def before_change(
    hooks: HookRegistry | None,
    context: HookContext | None,
    change: Any,
) -> None:
    """Dispatch a structural before callback when hooks are configured."""

    if hooks is not None:
        if context is None:
            raise RuntimeError("A configured hook registry requires an operation context.")
        hooks.before_structure_change(context, change)


def after_change(
    hooks: HookRegistry | None,
    context: HookContext | None,
    change: Any,
) -> None:
    """Dispatch a structural after callback when hooks are configured."""

    if hooks is not None:
        if context is None:
            raise RuntimeError("A configured hook registry requires an operation context.")
        hooks.after_structure_change(context, change)
