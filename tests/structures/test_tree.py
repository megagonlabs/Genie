"""Tests for the rooted ownership-tree facade."""

from __future__ import annotations

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryNotFound, MemoryPolicy
from genie.structures import OwnershipAccess, OwnershipTree


def test_tree_creates_children_under_root_or_selected_parent() -> None:
    """Child creation defaults to the root and supports nested members."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    tree = OwnershipTree(memory, root=root)

    child = tree.create_child(type="folder", value="child")
    grandchild = tree.create_child(
        parent=child,
        type="note",
        value="grandchild",
        metadata={"source": "test"},
    )

    assert child.parent_id == root.id
    assert grandchild.parent_id == child.id
    assert grandchild.metadata == {"source": "test"}
    assert tree.children() == [child]
    assert tree.children(child) == [grandchild]


def test_tree_traversal_is_bounded_at_the_facade_root() -> None:
    """Ancestors stop at the configured root even when another owner is above it."""

    memory = Memory()
    outer = memory.put(type="outer", value="outer")
    root = memory.put(parent=outer, type="root", value="root")
    first = memory.put(parent=root, type="node", value="first")
    second = memory.put(parent=root, type="node", value="second")
    leaf = memory.put(parent=first, type="node", value="leaf")
    tree = OwnershipTree(memory, root=root)

    assert tree.root == root
    assert tree.parent(root) is None
    assert tree.parent(leaf) == first
    assert tree.ancestors(leaf) == [first, root]
    assert tree.ancestors(leaf, include_self=True) == [leaf, first, root]
    assert tree.descendants() == [first, leaf, second]
    assert tree.descendants(first, include_self=True) == [first, leaf]
    assert outer not in tree.ancestors(leaf)


def test_tree_rejects_operations_outside_its_root() -> None:
    """A facade remains bounded even when it uses an unbounded Memory object."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    inside = memory.put(parent=root, type="node", value="inside")
    outside = memory.put(type="node", value="outside")
    tree = OwnershipTree(memory, root=root)

    assert tree.contains(root)
    assert tree.contains(inside.id)
    assert not tree.contains(outside)
    assert not tree.contains("missing")
    with pytest.raises(AccessDenied, match="outside ownership tree"):
        tree.children(outside)
    with pytest.raises(AccessDenied, match="outside ownership tree"):
        tree.create_child(parent=outside, type="note", value="escape")
    with pytest.raises(AccessDenied, match="outside ownership tree"):
        tree.delete_subtree(outside)


def test_tree_preserves_handle_upward_traversal_limits() -> None:
    """The facade does not bypass a MemoryHandle's allow_up setting."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="node", value="child")
    default_handle = memory.get_handle(root=root)
    default_tree = OwnershipTree(default_handle, root=root)

    assert default_tree.children() == [child]
    with pytest.raises(AccessDenied, match="does not allow ancestor traversal"):
        default_tree.ancestors(child)
    with pytest.raises(AccessDenied, match="does not allow ancestor traversal"):
        default_tree.parent(child)

    upward_handle = memory.get_handle(root=root, allow_up=True)
    upward_tree = OwnershipTree(upward_handle, root=root)
    assert upward_tree.ancestors(child) == [root]


def test_tree_returns_only_visible_children_and_descendants() -> None:
    """Traversal keeps the access object's normal block policy filtering."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    visible = owner.put(
        parent=root,
        type="node",
        value="visible",
        policy=MemoryPolicy.public(),
    )
    hidden = owner.put(
        parent=root,
        type="node",
        value="hidden",
        policy=MemoryPolicy(
            read=AccessRule.only(principals={"owner"}),
            traverse_down=AccessRule.only(principals={"owner"}),
        ),
    )
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )
    tree = OwnershipTree(reader, root=root)

    assert tree.children() == [visible]
    assert tree.descendants() == [visible]
    assert not tree.contains(hidden)


def test_tree_does_not_follow_normal_links() -> None:
    """Non-owning relationships do not change ownership-tree membership."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="node", value="child")
    linked = memory.put(type="node", value="linked")
    memory.add_link(child, linked, "related")
    tree = OwnershipTree(memory, root=root)

    assert tree.descendants() == [child]
    assert not tree.contains(linked)


def test_tree_delete_subtree_uses_existing_recursive_delete() -> None:
    """Deleting a member removes its owned descendants but keeps siblings."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    branch = memory.put(parent=root, type="branch", value="branch")
    leaf = memory.put(parent=branch, type="leaf", value="leaf")
    sibling = memory.put(parent=root, type="branch", value="sibling")
    tree = OwnershipTree(memory, root=root)

    tree.delete_subtree(branch)

    assert memory.get(branch.id) is None
    assert memory.get(leaf.id) is None
    assert tree.children() == [sibling]


def test_tree_delete_preserves_recursive_policy_checks() -> None:
    """A protected descendant prevents deletion of its owner branch."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    branch = memory.put(parent=root, type="branch", value="branch")
    protected = memory.put(
        parent=branch,
        type="leaf",
        value="protected",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )
    tree = OwnershipTree(memory, root=root)

    with pytest.raises(AccessDenied):
        tree.delete_subtree(branch)

    assert memory.get(branch.id) is not None
    assert memory.get(protected.id) is not None


def test_tree_requires_an_existing_visible_root() -> None:
    """Facade construction and later access fail clearly for a missing root."""

    memory = Memory()

    with pytest.raises(MemoryNotFound, match="tree root"):
        OwnershipTree(memory, root="missing")

    root = memory.put(type="root", value="root")
    tree = OwnershipTree(memory, root=root)
    tree.delete_subtree(root)

    with pytest.raises(MemoryNotFound, match="tree root"):
        _ = tree.root


def test_memory_and_handle_implement_ownership_access() -> None:
    """Both public access objects can be supplied to OwnershipTree."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)

    assert isinstance(memory, OwnershipAccess)
    assert isinstance(handle, OwnershipAccess)
