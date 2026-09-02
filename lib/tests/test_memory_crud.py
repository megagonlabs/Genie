"""Tests for core memory CRUD behavior."""

from __future__ import annotations

from typing import Any, cast

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryBlock, MemoryPolicy
from genie.memory.errors import MemoryConflict, MemoryNotFound


def test_put_get_and_update_memory_block() -> None:
    """Memory blocks can be created, read, updated, and versioned."""

    memory = Memory()

    block = memory.put(type="preference", value={"style": "concise"})
    updated = memory.update(block, value={"tone": "warm"}, metadata={"source": "test"})

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


def test_delete_authorizes_the_target_and_not_its_descendants() -> None:
    """A descendant that denies write no longer strands its owner branch."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    branch = memory.put(parent=root, type="branch", value="branch")
    locked = memory.put(
        parent=branch,
        type="leaf",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )

    memory.delete(root)

    assert memory.get(root.id) is None
    assert memory.get(branch.id) is None
    assert memory.get(locked.id) is None


def test_delete_requires_write_access_to_the_target() -> None:
    """A target that denies write refuses deletion and leaves the tree intact."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    locked = memory.put(
        parent=root,
        type="leaf",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )

    with pytest.raises(AccessDenied):
        memory.delete(locked)

    assert memory.get(locked.id) is not None
    assert [child.id for child in memory.children(root)] == [locked.id]


def test_missing_blocks_and_conflicting_ids_raise_clear_errors() -> None:
    """Missing ids and duplicate ids raise explicit memory errors."""

    memory = Memory()
    block = memory.put(type="note", value="exists", id="fixed")

    assert memory.get("missing") is None
    with pytest.raises(MemoryNotFound):
        memory.set("missing", "value")
    with pytest.raises(MemoryNotFound):
        memory.update("missing", value={"field": "value"})
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


def test_contains_enforces_root_traversal_and_candidate_read_policy() -> None:
    """Public containment does not expose private ownership membership."""

    owner = Memory(principal="owner")
    public_root = owner.put(
        type="root",
        value="public",
        policy=MemoryPolicy.public(),
    )
    private_child = owner.put(
        parent=public_root,
        type="note",
        value="private",
    )
    private_root = owner.put(type="root", value="private")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert owner.contains(public_root, private_child)
    assert reader.contains(public_root, private_child) is False
    assert reader.contains(public_root, "missing") is False
    with pytest.raises(AccessDenied):
        reader.contains(private_root, private_root)
    with pytest.raises(MemoryNotFound):
        reader.contains("missing", private_child)


def test_update_requires_dict_values() -> None:
    """Value updates are accepted only for dictionary values."""

    memory = Memory()
    block = memory.put(type="note", value="plain text")

    with pytest.raises(TypeError):
        memory.update(block, value={"field": "value"})


def test_metadata_only_update_preserves_value_and_versions() -> None:
    """Metadata-only updates leave values intact while versioning the block."""

    memory = Memory()
    block = memory.put(type="note", value=["keep"], metadata={"source": "initial"})

    updated = memory.update(block, metadata={"priority": 2})

    assert updated.value == ["keep"]
    assert updated.metadata == {"source": "initial", "priority": 2}
    assert updated.version == 2
    reread = memory.get(block)
    assert reread is not None
    assert reread.value == ["keep"]
    assert reread.metadata == {"source": "initial", "priority": 2}


def test_block_metadata_requires_a_strict_json_dictionary() -> None:
    """In-memory use enforces the same portable metadata shape as persistence."""

    memory = Memory()

    with pytest.raises(TypeError, match="dictionary"):
        memory.put(
            type="note",
            value="invalid",
            metadata=cast(dict[str, Any], []),
        )
    with pytest.raises(TypeError, match="strict JSON"):
        memory.put(
            type="note",
            value="invalid",
            metadata={"confidence": float("nan")},
        )

    block = memory.put(type="note", value={})
    with pytest.raises(TypeError, match="dictionary"):
        memory.update(block, metadata=cast(dict[str, Any], []))


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


def test_memory_block_defaults_and_updates_are_safe_value_objects() -> None:
    """Block helpers create unique UTC identities and normalize cleared metadata."""

    first = MemoryBlock(type="note", value="first", metadata={"source": "test"})
    second = MemoryBlock(type="note", value="second")

    cleared = first.with_updates(metadata=None)

    assert first.id.startswith("mem_")
    assert second.id.startswith("mem_")
    assert first.id != second.id
    assert first.created_at.utcoffset() is not None
    assert first.updated_at.utcoffset() is not None
    assert first.metadata == {"source": "test"}
    assert cleared.metadata == {}
