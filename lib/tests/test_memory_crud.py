"""Tests for core memory CRUD behavior."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest

from genie import AccessDenied, AccessRule, InMemoryBackend, Memory, MemoryBlock, MemoryPolicy, Operation, PolicyEngine, Principal
from genie.codecs import MAX_NESTING_DEPTH
from genie.memory.errors import MemoryConflict, MemoryNotFound


def test_put_get_and_update_memory_block() -> None:
    """`Memory` creates, reads, and updates a block, and each update increases its `version`."""

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
    """`set` replaces a block value, and a recursive `delete` removes the block, its descendants, and its links."""

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
    """A descendant that denies `write` does not block the delete of the branch that owns it."""

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
    """If the target denies `write`, `delete` raises `AccessDenied` and does not change the tree."""

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


def test_set_and_update_require_read_and_write_access() -> None:
    """A principal that can write but not read a block cannot change it, because `set` and `update` return the stored block."""

    owner = Memory(principal="owner")
    drop_box = MemoryPolicy(read=AccessRule.owner(), write=AccessRule.allow())
    box = owner.put(type="box", value={"secret": "owner only"}, metadata={"note": "private"}, policy=drop_box)
    shared = owner.put(type="note", value="shared", policy=MemoryPolicy(read=AccessRule.allow()))
    other = Memory(backend=owner.backend, principal="other")

    with pytest.raises(AccessDenied, match="cannot 'read'"):
        other.set(box, "overwritten")
    with pytest.raises(AccessDenied, match="cannot 'read'"):
        other.update(box, value={"hello": "hi"})
    with pytest.raises(AccessDenied, match="cannot 'read'"):
        other.update(box, metadata={"tampered": True})
    with pytest.raises(AccessDenied, match="cannot 'write'"):
        other.set(shared, "overwritten")

    assert owner.get(box) == box
    assert owner.get(shared) == shared
    assert owner.set(box, {"secret": "rotated"}).version == 2


def test_require_access_checks_a_policy_without_changing_the_block() -> None:
    """`require_access` raises `AccessDenied` when the policy denies access, and otherwise returns `None` and changes nothing."""

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
    """An unknown id raises `MemoryNotFound` (but `get` returns `None`), and a duplicate id raises `MemoryConflict`."""

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


class _InterleavedBackend(InMemoryBackend):
    """`InMemoryBackend` that runs a callback once, just before a selected backend call.

    The tests use it to put the work of a second writer at a fixed point inside
    one `Memory` operation, with no threads.
    """

    def __init__(self) -> None:
        """Start with no callbacks."""

        super().__init__()
        self.before: dict[str, Callable[[], None]] = {}

    def _run_before(self, method: str) -> None:
        """Remove the callback for one method, then run it, if it exists."""

        callback = self.before.pop(method, None)
        if callback is not None:
            callback()

    def update_block(self, block: MemoryBlock, *, expected_version: int | None = None) -> MemoryBlock:
        """Run the `update_block` callback, then update the block."""

        self._run_before("update_block")
        return super().update_block(block, expected_version=expected_version)

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Run the `descendants` callback, then read the subtree."""

        self._run_before("descendants")
        return super().descendants(block_id, include_self=include_self)

    def delete_block(self, block_id: str, *, expected_blocks: Sequence[MemoryBlock] | None = None) -> None:
        """Run the `delete_block` callback, then delete the subtree."""

        self._run_before("delete_block")
        super().delete_block(block_id, expected_blocks=expected_blocks)


def _shared_block_and_bob_creates_again(backend: _InterleavedBackend) -> tuple[Memory, Callable[[], None]]:
    """Return the memory of alice, who owns a block that bob can write, and a callback.

    In the callback, bob deletes that block. Then he creates his own
    `owner_private` block with the same id, and it also has version 1.
    """

    alice = Memory(backend=backend, principal="alice")
    bob = Memory(backend=backend, principal="bob")
    alice.put(type="doc", value={"text": "alice"}, id="shared", policy=MemoryPolicy.owner_or(principals={"bob"}))

    def bob_creates_again() -> None:
        bob.delete("shared")
        bob.put(type="doc", value={"text": "bob"}, id="shared")

    return alice, bob_creates_again


@pytest.mark.parametrize("operation", ["set", "update"])
def test_set_and_update_reject_a_block_created_again_with_the_same_id(operation: str) -> None:
    """If another principal replaces the block with a new block that has the same id and version before the write, the write raises `MemoryConflict` and the new block does not change."""

    backend = _InterleavedBackend()
    alice, bob_creates_again = _shared_block_and_bob_creates_again(backend)
    backend.before["update_block"] = bob_creates_again

    with pytest.raises(MemoryConflict):
        if operation == "set":
            alice.set("shared", {"text": "stale"})
        else:
            alice.update("shared", value={"text": "stale"})

    stored = backend.get_block("shared")
    assert stored is not None
    assert (stored.value, stored.created_by, stored.version) == ({"text": "bob"}, "bob", 1)
    assert not PolicyEngine().allows(Operation.READ, stored, Principal("alice"))


@pytest.mark.parametrize("step", ["descendants", "delete_block"])
def test_delete_rejects_a_block_created_again_with_the_same_id(step: str) -> None:
    """`delete` authorizes one block. If another principal replaces that block before the subtree read or before the commit, the delete raises `MemoryConflict` and the new block stays."""

    backend = _InterleavedBackend()
    alice, bob_creates_again = _shared_block_and_bob_creates_again(backend)
    backend.before[step] = bob_creates_again

    with pytest.raises(MemoryConflict):
        alice.delete("shared")

    stored = backend.get_block("shared")
    assert stored is not None
    assert (stored.value, stored.created_by) == ({"text": "bob"}, "bob")


def test_contains_enforces_root_traversal_and_candidate_read_policy() -> None:
    """`contains` does not tell a reader that a private block is in a subtree."""

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
    """`update` accepts a new value only when the stored value is a dictionary."""

    memory = Memory()
    block = memory.put(type="note", value="plain text")

    with pytest.raises(TypeError):
        memory.update(block, value={"field": "value"})


def test_metadata_only_update_preserves_value_and_versions() -> None:
    """An `update` with only metadata keeps the value and increases the block `version`."""

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
    """In-memory use applies the same strict JSON dictionary rule for metadata as persistent backends."""

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


def test_block_metadata_nested_past_the_limit_is_a_value_error() -> None:
    """Metadata can nest to `MAX_NESTING_DEPTH` levels, with the metadata dictionary as level 1."""

    memory = Memory()
    root = memory.put(type="root", value={})
    handle = memory.get_handle(root=root)
    at_limit = {"tags": json.loads("[" * (MAX_NESTING_DEPTH - 1) + "]" * (MAX_NESTING_DEPTH - 1))}
    too_deep = {"tags": json.loads("[" * MAX_NESTING_DEPTH + "]" * MAX_NESTING_DEPTH)}

    block = memory.put(type="note", value="kept", metadata=at_limit)

    assert block.metadata == at_limit
    for write in (
        lambda: memory.put(type="note", value="invalid", metadata=too_deep),
        lambda: handle.put(type="note", value="invalid", metadata=too_deep),
        lambda: memory.update(block, metadata=too_deep),
    ):
        with pytest.raises(ValueError, match=f"metadata must not nest lists and dictionaries more than {MAX_NESTING_DEPTH} levels deep"):
            write()
    assert memory.get(block.id) == block
    assert memory.children(root) == []


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
    """`MemoryBlock` creates a unique id and UTC timestamps, and `with_updates(metadata=None)` sets metadata to `{}`."""

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
    """`put` rejects a bad block type, id, or parent before it stores anything."""

    memory = Memory()

    with pytest.raises(error, match=message):
        memory.put(value="invalid", **arguments)

    assert list(memory.search()) == []


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
    """`MemoryBlock` rejects bad identity fields, and `with_updates` also rejects them."""

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
