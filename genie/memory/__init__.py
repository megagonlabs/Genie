"""Core memory models and public memory handles."""

from typing import TYPE_CHECKING

from genie.memory.block import MemoryBlock
from genie.memory.errors import GenieError, MemoryConflict, MemoryNotFound, UnsupportedOperation
from genie.memory.links import LinkRelations, MemoryLink

if TYPE_CHECKING:
    from genie.memory.memory import Memory
    from genie.memory.scope import MemoryScope

__all__ = [
    "GenieError",
    "LinkRelations",
    "Memory",
    "MemoryBlock",
    "MemoryConflict",
    "MemoryLink",
    "MemoryNotFound",
    "MemoryScope",
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
    if name == "MemoryScope":
        from genie.memory.scope import MemoryScope

        return MemoryScope
    raise AttributeError(name)
