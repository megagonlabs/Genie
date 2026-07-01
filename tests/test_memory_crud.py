"""Tests for core memory CRUD behavior."""

from __future__ import annotations

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryPolicy
from genie.memory.errors import MemoryConflict, MemoryNotFound


def test_put_get_and_patch_memory_block() -> None:
    """Memory blocks can be created, read, patched, and versioned."""

    memory = Memory()

    block = memory.put(type="preference", value={"style": "concise"})
    updated = memory.patch(block, value={"tone": "warm"}, metadata={"source": "test"})

    assert updated.value == {"style": "concise", "tone": "warm"}
    assert updated.metadata["source"] == "test"
    assert updated.version == 2
    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value["tone"] == "warm"


def test_set_delete_and_recursive_cleanup() -> None:
    """Set and recursive delete update blocks, children, and links."""

    memory = Memory()
    root = memory.put(type="root", value="old")
    child = memory.put(parent=root, type="child", value="child")
    grandchild = memory.put(parent=child, type="grandchild", value="grandchild")
    memory.add_link(root, child, "contains")

    updated = memory.set(root, "new")
    assert updated.value == "new"
    assert updated.version == 2

    memory.delete(child)

    assert memory.get(child.id) is None
    assert memory.get(grandchild.id) is None
    assert memory.children(root) == []
    assert memory.list_links(root) == []


def test_delete_requires_write_access_for_descendants() -> None:
    """Recursive delete fails if any descendant denies write access."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(
        parent=root,
        type="child",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )

    with pytest.raises(AccessDenied):
        memory.delete(root)

    assert memory.get(root.id) is not None
    assert memory.get(child.id) is not None


def test_missing_blocks_and_conflicting_ids_raise_clear_errors() -> None:
    """Missing ids and duplicate ids raise explicit memory errors."""

    memory = Memory()
    block = memory.put(type="note", value="exists", id="fixed")

    assert memory.get("missing") is None
    with pytest.raises(MemoryNotFound):
        memory.set("missing", "value")
    with pytest.raises(MemoryNotFound):
        memory.patch("missing", value={"field": "value"})
    with pytest.raises(MemoryNotFound):
        memory.children("missing")
    with pytest.raises(MemoryNotFound):
        memory.ancestors("missing")
    with pytest.raises(MemoryNotFound):
        memory.list_links("missing")
    with pytest.raises(MemoryNotFound):
        memory.discover_mounts("missing")
    with pytest.raises(MemoryNotFound):
        memory.delete("missing")
    with pytest.raises(MemoryConflict):
        memory.put(type="note", value="duplicate", id=block.id)
    with pytest.raises(MemoryNotFound):
        memory.put(type="note", value="orphan", parent="missing")


def test_patch_requires_dict_values() -> None:
    """Value patches are accepted only for dictionary values."""

    memory = Memory()
    block = memory.put(type="note", value="plain text")

    with pytest.raises(TypeError):
        memory.patch(block, value={"field": "value"})


def test_metadata_only_patch_preserves_value_and_versions() -> None:
    """Metadata-only patches leave values intact while versioning the block."""

    memory = Memory()
    block = memory.put(type="note", value=["keep"], metadata={"source": "initial"})

    updated = memory.patch(block, metadata={"priority": 2})

    assert updated.value == ["keep"]
    assert updated.metadata == {"source": "initial", "priority": 2}
    assert updated.version == 2
    reread = memory.get(block)
    assert reread is not None
    assert reread.value == ["keep"]
    assert reread.metadata == {"source": "initial", "priority": 2}


def test_returned_blocks_do_not_mutate_stored_values_or_metadata() -> None:
    """Returned block values and metadata are defensive copies."""

    memory = Memory()
    block = memory.put(
        type="note",
        value={"items": ["original"]},
        metadata={"tags": ["source"]},
    )

    block.value["items"].append("mutated")
    block.metadata["tags"].append("mutated")

    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value == {"items": ["original"]}
    assert retrieved.metadata == {"tags": ["source"]}

    retrieved.value["items"].append("again")
    retrieved.metadata["tags"].append("again")
    reread = memory.get(block.id)
    assert reread is not None
    assert reread.value == {"items": ["original"]}
    assert reread.metadata == {"tags": ["source"]}
