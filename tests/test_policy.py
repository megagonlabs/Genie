"""Tests for principals, roles, and block-level policies."""

from __future__ import annotations

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryPolicy, Principal


def test_block_policy_denies_read_to_other_principal() -> None:
    """Read policies deny principals that are not explicitly allowed."""

    owner_memory = Memory(principal="owner")
    private_policy = MemoryPolicy(read=AccessRule.only(principals={"owner"}))
    private = owner_memory.put(type="secret", value="hidden", policy=private_policy)

    other_memory = Memory(
        backend=owner_memory.backend,
        principal="other",
        policy_engine=owner_memory.policy_engine,
        hooks=owner_memory.hooks,
        codecs=owner_memory.codecs,
    )

    with pytest.raises(AccessDenied):
        other_memory.get(private.id)


def test_write_policy_blocks_set_patch_and_delete() -> None:
    """Write policies protect set, patch, and delete operations."""

    owner = Memory(principal="owner")
    block = owner.put(
        type="note",
        value={"body": "original"},
        policy=MemoryPolicy(write=AccessRule.only(principals={"owner"})),
    )
    writer = Memory(
        backend=owner.backend,
        principal="writer",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        writer.set(block, {"body": "set"})
    with pytest.raises(AccessDenied):
        writer.patch(block, value={"body": "patched"})
    with pytest.raises(AccessDenied):
        writer.delete(block)

    retrieved = owner.get(block)
    assert retrieved is not None
    assert retrieved.value == {"body": "original"}


def test_children_ancestors_and_search_filter_inaccessible_blocks() -> None:
    """Traversal and search filter results by read and search policies."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    public_child = owner.put(parent=root, type="note", value="needle public")
    owner.put(
        parent=root,
        type="note",
        value="needle private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    unsearchable_child = owner.put(
        parent=root,
        type="note",
        value="needle unsearchable",
        policy=MemoryPolicy(search=AccessRule.only(principals={"owner"})),
    )
    private_parent = owner.put(
        type="root",
        value="private parent",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    public_leaf = owner.put(parent=private_parent, type="note", value="public leaf")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert [block.id for block in reader.children(root)] == [public_child.id, unsearchable_child.id]
    assert reader.ancestors(public_leaf) == []
    assert [result.block.id for result in reader.search("needle", root=root, scope="subtree")] == [
        public_child.id
    ]


def test_policy_roles_allow_access_without_principal_id_match() -> None:
    """Role-based policies allow matching roles without id matches."""

    owner = Memory(principal="owner")
    policy = MemoryPolicy(read=AccessRule.only(roles={"reader"}))
    block = owner.put(type="note", value="role gated", policy=policy)
    reader = Memory(
        backend=owner.backend,
        principal=Principal(id="agent", roles=frozenset({"reader"})),
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    retrieved = reader.get(block)

    assert retrieved is not None
    assert retrieved.value == "role gated"


def test_policy_can_deny_parent_writes_and_traversal() -> None:
    """Policies can deny child writes and downward traversal."""

    memory = Memory()
    parent = memory.put(
        type="root",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny(), traverse_down=AccessRule.deny()),
    )

    with pytest.raises(AccessDenied):
        memory.put(parent=parent, type="child", value="blocked")
    with pytest.raises(AccessDenied):
        memory.children(parent)
