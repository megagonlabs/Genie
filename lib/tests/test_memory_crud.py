"""Tests for core memory CRUD behavior."""

from __future__ import annotations

from typing import Any, cast

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryBlock, MemoryPolicy, Operation, PolicyEngine
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


def test_require_access_checks_a_policy_without_changing_the_block() -> None:
    """`require_access` raises `AccessDenied` on refusal and otherwise does nothing."""

    engine = PolicyEngine(operations={"export": AccessRule.owner()})
    owner = Memory(principal="owner", policy_engine=engine)
    shared = owner.put(type="note", value="shared", policy=MemoryPolicy(read=AccessRule.allow()))
    reader = Memory(backend=owner.backend, principal="reader", policy_engine=engine)

    assert owner.require_access(Operation.WRITE, shared) is None
    assert owner.require_access("export", shared.id) is None
    assert reader.require_access("read", shared) is None
    with pytest.raises(AccessDenied):
        reader.require_access(Operation.WRITE, shared)
    with pytest.raises(AccessDenied):
        reader.require_access("export", shared)
    with pytest.raises(MemoryNotFound):
        reader.require_access(Operation.READ, "missing")
    with pytest.raises(ValueError):
        reader.require_access("unknown", shared)
    with pytest.raises(ValueError):
        reader.require_access(Operation.READ, "")
    with pytest.raises(TypeError):
        reader.require_access(Operation.READ, cast(Any, 123))
    assert owner.get(shared) == shared


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


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"type": 123}, TypeError, "type must be a string"),
        ({"type": ""}, ValueError, "type must not be empty"),
        ({"type": "   "}, ValueError, "type must not be empty"),
        ({"type": "note", "id": 123}, TypeError, "id must be a string"),
        ({"type": "note", "id": {"$ne": None}}, TypeError, "id must be a string"),
        ({"type": "note", "id": ""}, ValueError, "id must not be empty"),
        ({"type": "note", "parent": 123}, TypeError, "MemoryBlock or a string id"),
        ({"type": "note", "parent": ""}, ValueError, "Block id must not be empty"),
    ],
)
def test_put_rejects_invalid_type_id_and_parent(
    arguments: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """A bad block type, id, or parent fails before anything is stored."""

    memory = Memory()

    with pytest.raises(error, match=message):
        memory.put(value="invalid", **arguments)

    assert memory.search() == []


def test_block_references_must_be_blocks_or_non_empty_string_ids() -> None:
    """Every block argument rejects a bad id before the backend reads it."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    calls = {
        "get": lambda ref: memory.get(ref),
        "set": lambda ref: memory.set(ref, "value"),
        "update": lambda ref: memory.update(ref, metadata={}),
        "delete": lambda ref: memory.delete(ref),
        "children": lambda ref: memory.children(ref),
        "ancestors": lambda ref: memory.ancestors(ref),
        "contains root": lambda ref: memory.contains(ref, root),
        "contains block": lambda ref: memory.contains(root, ref),
        "search": lambda ref: memory.search(root=ref, scope="subtree"),
        "get_handle": lambda ref: memory.get_handle(root=ref),
    }

    optional_root = {"search", "get_handle"}

    for name, call in calls.items():
        for ref in (123, {"$ne": None}, None):
            if ref is None and name in optional_root:
                continue
            with pytest.raises(TypeError, match="MemoryBlock or a string id"):
                call(ref)
        with pytest.raises(ValueError, match="Block id must not be empty"):
            call("")


def test_memory_block_requires_string_id_type_and_parent() -> None:
    """The block record rejects bad identity fields, including in copies."""

    block = MemoryBlock(type="note", value="value")

    with pytest.raises(TypeError, match="MemoryBlock type must be a string"):
        MemoryBlock(type=cast(str, 123), value="value")
    with pytest.raises(ValueError, match="MemoryBlock type must not be empty"):
        MemoryBlock(type="", value="value")
    with pytest.raises(TypeError, match="MemoryBlock id must be a string"):
        MemoryBlock(type="note", value="value", id=cast(str, 123))
    with pytest.raises(ValueError, match="MemoryBlock id must not be empty"):
        MemoryBlock(type="note", value="value", id=" ")
    with pytest.raises(TypeError, match="MemoryBlock parent_id must be a string"):
        MemoryBlock(type="note", value="value", parent_id=cast(str, 123))
    with pytest.raises(ValueError, match="MemoryBlock parent_id must not be empty"):
        MemoryBlock(type="note", value="value", parent_id="")
    with pytest.raises(TypeError, match="MemoryBlock id must be a string"):
        block.with_updates(id=None)
    assert MemoryBlock(type="note", value="value", parent_id=None).parent_id is None
