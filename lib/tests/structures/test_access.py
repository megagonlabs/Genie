"""Tests for shared structure access protocols."""

from __future__ import annotations

from genie import Memory
from genie.structures import (
    AcyclicLinkWriter,
    BlockReader,
    BlockWriter,
    LinkReader,
    LinkWriter,
    MemoryAccess,
    SearchAccess,
    TreeReader,
)


def test_memory_implements_shared_structure_access_protocols() -> None:
    """The trusted Memory API satisfies every shared access protocol."""

    memory = Memory()

    assert isinstance(memory, BlockReader)
    assert isinstance(memory, BlockWriter)
    assert isinstance(memory, TreeReader)
    assert isinstance(memory, LinkReader)
    assert isinstance(memory, LinkWriter)
    assert isinstance(memory, AcyclicLinkWriter)
    assert isinstance(memory, SearchAccess)
    assert isinstance(memory, MemoryAccess)


def test_memory_handle_implements_shared_structure_access_protocols() -> None:
    """A scoped `MemoryHandle` can be passed to the same structure facades."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)

    assert isinstance(handle, BlockReader)
    assert isinstance(handle, BlockWriter)
    assert isinstance(handle, TreeReader)
    assert isinstance(handle, LinkReader)
    assert isinstance(handle, LinkWriter)
    assert isinstance(handle, AcyclicLinkWriter)
    assert isinstance(handle, SearchAccess)
    assert isinstance(handle, MemoryAccess)
