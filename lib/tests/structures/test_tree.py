"""Tests for the rooted ownership-tree facade."""

from __future__ import annotations

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryNotFound,
    MemoryPolicy,
)
from genie.structures import OwnershipAccess, OwnershipTree


def test_tree_creates_children_under_root_or_selected_parent() -> None:
    """`create_child` uses the root as the default parent and can also create nested members."""

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
    assert tree.ancestors(root) == []
    assert tree.parent(leaf) == first
    assert tree.ancestors(leaf) == [first, root]
    assert tree.ancestors(leaf, include_self=True) == [leaf, first, root]
    assert tree.descendants() == [first, leaf, second]
    assert tree.descendants(first, include_self=True) == [first, leaf]
    assert outer not in tree.ancestors(leaf)


def test_tree_detects_inconsistent_ancestor_and_child_indexes() -> None:
    """The facade fails closed and raises `AccessDenied` or `MemoryConflict` when an adapter returns broken ownership indexes."""

    class CorruptTreeBackend(InMemoryBackend):
        """Let the test make single ownership index reads inconsistent."""

        broken_ancestor_id: str | None = None
        repeated_child_id: str | None = None
        repeated_root_id: str | None = None

        def ancestors(self, block_id: str) -> list[MemoryBlock]:
            """Hide a configured ancestor chain."""

            if block_id == self.broken_ancestor_id:
                return []
            return super().ancestors(block_id)

        def children(self, block_id: str) -> list[MemoryBlock]:
            """Repeat the root under a configured child."""

            children = super().children(block_id)
            if block_id == self.repeated_child_id and self.repeated_root_id is not None:
                repeated = self.get_block(self.repeated_root_id)
                assert repeated is not None
                children.append(repeated)
            return children

    backend = CorruptTreeBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="node", value="child")
    tree = OwnershipTree(memory, root=root)

    backend.broken_ancestor_id = child.id
    with pytest.raises(AccessDenied, match="Cannot traverse"):
        tree.ancestors(child)

    backend.broken_ancestor_id = None
    backend.repeated_child_id = child.id
    backend.repeated_root_id = root.id
    with pytest.raises(MemoryConflict, match="repeated memory block"):
        tree.descendants()


def test_tree_rejects_operations_outside_its_root() -> None:
    """The facade stays bounded to its root even when it uses an unbounded `Memory` object."""

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
    """The facade does not bypass the `allow_up` flag of a `MemoryHandle`."""

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
    """Tree traversal applies the normal block policy, so a reader does not see a hidden child."""

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


def test_tree_ignores_the_traversal_policy_of_other_branches() -> None:
    """A branch that denies `traverse_down` does not block other branches, and `descendants` skips only its children.

    Policies do not inherit. So a readable block below that branch is still a member.
    """

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    closed = owner.put(
        parent=root,
        type="branch",
        value="closed",
        policy=MemoryPolicy(
            read=AccessRule.allow(),
            write=AccessRule.owner(),
            search=AccessRule.allow(),
            traverse_up=AccessRule.allow(),
            traverse_down=AccessRule.owner(),
        ),
    )
    closed_child = owner.put(parent=closed, type="node", value="closed child", policy=MemoryPolicy.public())
    other = Memory(backend=owner.backend, principal="other")
    branch = other.put(parent=root, type="branch", value="open", policy=MemoryPolicy.public())
    tree = OwnershipTree(other, root=root)

    leaf = tree.create_child(parent=branch, type="node", value="leaf")

    assert tree.children(branch) == [leaf]
    assert tree.parent(leaf) == branch
    assert tree.ancestors(leaf) == [branch, root]
    assert tree.descendants(branch) == [leaf]
    assert tree.descendants() == [closed, branch, leaf]
    assert tree.contains(leaf)
    assert tree.contains(closed_child)
    with pytest.raises(AccessDenied, match="cannot 'traverse_down'"):
        tree.children(closed)
    with pytest.raises(AccessDenied, match="cannot 'traverse_down'"):
        tree.descendants(closed)

    tree.delete_subtree(leaf)

    assert tree.children(branch) == []


def test_tree_member_below_an_unreadable_block_has_no_visible_parent() -> None:
    """A readable block below an unreadable block is a member, but `parent` gives `None` and `descendants` does not reach it."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    hidden = owner.put(parent=root, type="node", value="hidden")
    visible = owner.put(parent=hidden, type="node", value="visible", policy=MemoryPolicy.public())
    tree = OwnershipTree(Memory(backend=owner.backend, principal="reader"), root=root)

    assert tree.contains(visible)
    assert not tree.contains(hidden)
    assert tree.parent(visible) is None
    assert tree.ancestors(visible) == [root]
    assert tree.descendants() == []


def test_tree_member_checks_do_not_walk_the_tree() -> None:
    """A member check asks the access object for `contains`, so its cost does not grow with the tree size."""

    class CountingBackend(InMemoryBackend):
        """Count the child lookups."""

        children_calls = 0

        def children(self, block_id: str) -> list[MemoryBlock]:
            """Count one child lookup."""

            self.children_calls += 1
            return super().children(block_id)

    backend = CountingBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root")
    nodes = [memory.put(parent=root, type="node", value=index) for index in range(20)]
    tree = OwnershipTree(memory, root=root)
    backend.children_calls = 0

    note = tree.create_child(parent=nodes[-1], type="note", value="note")
    assert tree.parent(note) == nodes[-1]
    assert tree.ancestors(note) == [nodes[-1], root]
    assert tree.contains(note)
    assert tree.children(nodes[-1]) == [note]

    assert backend.children_calls == 1


def test_tree_does_not_follow_normal_links() -> None:
    """A normal link does not make a block a member of the ownership tree."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="node", value="child")
    linked = memory.put(type="node", value="linked")
    memory.add_link(child, linked, "related")
    tree = OwnershipTree(memory, root=root)

    assert tree.descendants() == [child]
    assert not tree.contains(linked)


def test_tree_delete_subtree_uses_existing_recursive_delete() -> None:
    """`delete_subtree` removes a member and its owned descendants, but keeps its siblings."""

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


def test_tree_delete_authorizes_the_target_branch_only() -> None:
    """`delete_subtree` checks `write` on the target branch only, not on its protected descendants."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    protected_branch = memory.put(
        parent=root,
        type="branch",
        value="protected",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )
    branch = memory.put(parent=root, type="branch", value="branch")
    leaf = memory.put(
        parent=branch,
        type="leaf",
        value="protected leaf",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )
    tree = OwnershipTree(memory, root=root)

    tree.delete_subtree(branch)

    assert memory.get(branch.id) is None
    assert memory.get(leaf.id) is None

    with pytest.raises(AccessDenied):
        tree.delete_subtree(protected_branch)

    assert memory.get(protected_branch.id) is not None


def test_tree_requires_an_existing_visible_root() -> None:
    """The constructor and later reads of `root` raise `MemoryNotFound` if the root does not exist."""

    memory = Memory()

    with pytest.raises(MemoryNotFound, match="tree root"):
        OwnershipTree(memory, root="missing")

    root = memory.put(type="root", value="root")
    tree = OwnershipTree(memory, root=root)
    tree.delete_subtree(root)

    with pytest.raises(MemoryNotFound, match="tree root"):
        _ = tree.root


def test_memory_and_handle_implement_ownership_access() -> None:
    """`Memory` and `MemoryHandle` both satisfy `OwnershipAccess`, so `OwnershipTree` accepts both."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)

    assert isinstance(memory, OwnershipAccess)
    assert isinstance(handle, OwnershipAccess)
