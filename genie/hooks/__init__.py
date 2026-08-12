"""Hook interfaces, events, and dispatch registry."""

from genie.hooks.base import MemoryHook
from genie.hooks.events import (
    HookContext,
    HookOperation,
    HookOrigin,
    StructureAction,
    StructureChange,
    StructureKind,
)
from genie.hooks.registry import HookRegistry
from genie.memory.errors import HookRecursionError

__all__ = [
    "HookContext",
    "HookOperation",
    "HookOrigin",
    "HookRecursionError",
    "HookRegistry",
    "MemoryHook",
    "StructureAction",
    "StructureChange",
    "StructureKind",
]
