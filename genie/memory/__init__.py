"""Core memory models and public memory handles."""

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink

__all__ = ["Memory", "MemoryBlock", "MemoryLink", "MemoryScope"]


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
