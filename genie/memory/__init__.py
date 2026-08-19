"""Core memory models and public memory handles."""

from typing import TYPE_CHECKING

from genie.memory.block import MemoryBlock
from genie.memory.errors import (
    BackendBusy,
    BackendCapacityExceeded,
    BackendCorruption,
    BackendError,
    BackendReadOnly,
    BackendUnavailable,
    GenieError,
    HookRecursionError,
    MemoryConflict,
    MemoryNotFound,
    UnsupportedOperation,
)
from genie.memory.links import LinkRelations, MemoryLink

if TYPE_CHECKING:
    from genie.memory.handle import MemoryHandle
    from genie.memory.memory import Memory

__all__ = [
    "BackendBusy",
    "BackendCapacityExceeded",
    "BackendCorruption",
    "BackendError",
    "BackendReadOnly",
    "BackendUnavailable",
    "GenieError",
    "HookRecursionError",
    "LinkRelations",
    "Memory",
    "MemoryBlock",
    "MemoryConflict",
    "MemoryHandle",
    "MemoryLink",
    "MemoryNotFound",
    "UnsupportedOperation",
]


def __getattr__(name: str):
    """Lazily import heavy memory handles to avoid circular imports.

    Args:
        name: Exported attribute name.

    Returns:
        The requested memory class.

    Raises:
        AttributeError: If the name is not exported by this module.
    """

    if name == "Memory":
        from genie.memory.memory import Memory

        return Memory
    if name == "MemoryHandle":
        from genie.memory.handle import MemoryHandle

        return MemoryHandle
    raise AttributeError(name)
