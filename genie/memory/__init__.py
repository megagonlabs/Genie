from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink

__all__ = ["Memory", "MemoryBlock", "MemoryLink", "MemoryScope"]


def __getattr__(name: str):
    if name == "Memory":
        from genie.memory.memory import Memory

        return Memory
    if name == "MemoryScope":
        from genie.memory.scope import MemoryScope

        return MemoryScope
    raise AttributeError(name)
