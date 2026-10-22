"""Tests for bounded memory handles and restricted access."""

from __future__ import annotations

from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    Memory,
    MemoryNotFound,
    MemoryPolicy,
    MemoryQuery,
    Operation,
    Scope,
)


def test_subtree_scope_limits_search_scope() -> None:
    """A subtree scope searches only below its root by default."""

    memory = Memory()
    root = memory.put(type="root", value="workspace")
    task = memory.put(parent=root, type="task", value="launch task")
    memory.put(parent=task, type="note", value="subtree only clue")
    memory.put(type="note", value="global only clue")

    scope = memory.get_handle(root=task)

    assert [result.block.value for result in scope.search("clue")] == ["subtree only clue"]
    with pytest.raises(AccessDenied):
        scope.search("clue", scope="global")


def test_scope_can_optionally_search_ancestors() -> None:
    """Only a handle with `allow_up=True` can search ancestors."""

    memory = Memory()
    root = memory.put(type="root", value="project launch context")
    task = memory.put(parent=root, type="task", value="implementation task")

    restricted = memory.get_handle(root=task)
    with pytest.raises(AccessDenied):
        restricted.search("project", scope="ancestors")

    allowed = memory.get_handle(root=task, allow_up=True)
    results = allowed.search("project", scope="ancestors")

    assert [result.block.id for result in results] == [root.id]


def test_child_handles_can_narrow_but_not_escape_parent_handle() -> None:
    """Child handles can narrow roots but cannot escape their parent handle."""

    memory = Memory()
    project = memory.put(type="project", value="project")
    task = memory.put(parent=project, type="task", value="task")
    step = memory.put(parent=task, type="step", value="step")
    sibling = memory.put(parent=project, type="task", value="sibling")
    outside = memory.put(type="project", value="outside")

    project_handle = memory.get_handle(root=project)
    task_handle = project_handle.get_handle(root=task)
    child = task_handle.put(type="observation", value="scoped child")

    assert child.parent_id == task.id
    retrieved = task_handle.get(step)
    assert retrieved is not None
    assert retrieved.value == "step"

    with pytest.raises(AccessDenied):
        task_handle.get(sibling)
    with pytest.raises(AccessDenied):
        project_handle.get_handle(root=outside)


def test_scope_put_and_set_forward_codec_options() -> None:
    """Scoped writes accept the same explicit codec options as full memory writes."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    scope = memory.get_handle(root=root)

    child = scope.put(
        type="note",
        value={"status": "draft"},
        content_type="application/json",
        id="note:explicit",
    )
    updated = scope.set(child, {"status": "done"}, content_type="application/json")

    assert child.id == "note:explicit"
    assert child.content_type == "application/json"
    assert updated.content_type == "application/json"
    assert updated.value == {"status": "done"}


def test_handle_put_rejects_explicit_policy_by_default() -> None:
    """A handle uses the default policy of `Memory` and rejects an explicit `policy` unless `allow_policy_override` is true."""

    memory = Memory()
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    writer_scope = memory.get_handle(root=root, principal="agent:writer")

    with pytest.raises(AccessDenied):
        writer_scope.put(
            type="note",
            value="private",
            policy=MemoryPolicy(read=AccessRule.only(principals={"agent:allowed"})),
        )

    child = writer_scope.put(type="note", value="writer-owned")
    allowed_scope = memory.get_handle(root=root, principal="agent:allowed")

    with pytest.raises(AccessDenied):
        allowed_scope.get(child)


def test_handle_put_forwards_block_policy_when_override_is_allowed() -> None:
    """A handle with `allow_policy_override=True` can attach a policy object, as `Memory.put` can."""

    memory = Memory()
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    writer_scope = memory.get_handle(
        root=root,
        principal="agent:writer",
        allow_policy_override=True,
    )

    private = writer_scope.put(
        type="note",
        value="private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"agent:allowed"})),
    )

    blocked_scope = memory.get_handle(root=root, principal="agent:blocked")
    allowed_scope = memory.get_handle(root=root, principal="agent:allowed")

    with pytest.raises(AccessDenied):
        blocked_scope.get(private)
    visible = allowed_scope.get(private)
    assert visible is not None
    assert visible.value == "private"


def test_child_handles_cannot_add_policy_override_access() -> None:
    """Child handles cannot gain explicit policy control unless the parent has it."""

    memory = Memory()
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    child = memory.put(parent=root, type="task", value="task", policy=MemoryPolicy.public())
    restricted = memory.get_handle(root=root, principal="agent:writer")

    with pytest.raises(AccessDenied):
        restricted.get_handle(root=child, allow_policy_override=True)

    privileged = memory.get_handle(
        root=root,
        principal="agent:writer",
        allow_policy_override=True,
    )
    narrowed = privileged.get_handle(root=child, allow_policy_override=True)

    assert narrowed.allow_policy_override is True


def test_scope_search_accepts_full_search_arguments() -> None:
    """Scoped search exposes text, type, filter, and limit arguments."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    scope = memory.get_handle(root=root)
    memory.put(parent=root, type="note", value="needle first", metadata={"priority": 1})
    memory.put(parent=root, type="note", value="needle second", metadata={"priority": 2})
    memory.put(parent=root, type="task", value="needle task", metadata={"priority": 3})

    results = scope.search(
        text="needle",
        types=["note"],
        filters={"priority": {">=": 2}},
        limit=1,
    )

    assert [result.block.value for result in results] == ["needle second"]


def test_child_handles_cannot_widen_parent_global_or_ancestor_access() -> None:
    """Child handles cannot add global or ancestor permissions."""

    memory = Memory()
    root = memory.put(type="root", value="root context")
    task = memory.put(parent=root, type="task", value="task")
    memory.put(type="note", value="global clue")

    restricted = memory.get_handle(root=task)

    with pytest.raises(AccessDenied):
        restricted.get_handle(allow_up=True)
    with pytest.raises(AccessDenied):
        restricted.get_handle(allow_global=True)

    privileged = memory.get_handle(root=task, allow_up=True, allow_global=True)
    child_handle = privileged.get_handle(allow_up=True, allow_global=True)

    assert [result.block.value for result in child_handle.search("root", scope="ancestors")] == [
        "root context"
    ]
    assert [result.block.value for result in child_handle.search("global", scope="global")] == [
        "global clue"
    ]


def test_handle_creation_rejects_default_scope_that_requires_disabled_access() -> None:
    """A new handle rejects a default scope that needs `allow_global` or `allow_up` if that flag is off."""

    memory = Memory()
    root = memory.put(type="root", value="root context")

    with pytest.raises(ValueError, match="allow_global"):
        memory.get_handle(root=root, default_scope="global")
    with pytest.raises(ValueError, match="allow_up"):
        memory.get_handle(root=root, default_scope=Scope.ANCESTORS)
    with pytest.raises(ValueError, match="allow_global"):
        memory.get_unbounded_handle(default_scope="global")

    global_handle = memory.get_handle(root=root, default_scope="global", allow_global=True)
    ancestor_handle = memory.get_handle(root=root, default_scope="ancestors", allow_up=True)

    assert global_handle.default_scope == Scope.GLOBAL
    assert ancestor_handle.default_scope == Scope.ANCESTORS


def test_child_handle_creation_rejects_default_scope_without_matching_child_capability() -> None:
    """A child handle rejects a default scope unless the child itself enables the capability that scope needs."""

    memory = Memory()
    root = memory.put(type="root", value="root context")
    parent = memory.get_handle(root=root, allow_up=True, allow_global=True)

    with pytest.raises(ValueError, match="allow_global"):
        parent.get_handle(default_scope="global")
    with pytest.raises(ValueError, match="allow_up"):
        parent.get_handle(default_scope="ancestors")

    global_child = parent.get_handle(default_scope="global", allow_global=True)
    ancestor_child = parent.get_handle(default_scope="ancestors", allow_up=True)

    assert global_child.default_scope == Scope.GLOBAL
    assert ancestor_child.default_scope == Scope.ANCESTORS


def test_scope_search_cannot_escape_through_memory_query_objects() -> None:
    """A prebuilt `MemoryQuery` cannot pass a wider root or scope through a handle."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")
    outside = memory.put(type="note", value="outside needle")
    scope = memory.get_handle(root=root)

    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="outside", root_id=outside.id, scope=Scope.SELF))
    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="outside", scope=Scope.GLOBAL))

    upward_scope = memory.get_handle(root=child, allow_up=True)
    with pytest.raises(AccessDenied):
        upward_scope.search(MemoryQuery(text="outside", root_id=outside.id, scope=Scope.ANCESTORS))

    global_scope = memory.get_handle(root=root, allow_global=True)
    results = global_scope.search(MemoryQuery(text="outside", scope=Scope.GLOBAL))

    assert [result.block.id for result in results] == [outside.id]


def test_child_handles_cannot_change_principal_identity() -> None:
    """A child handle cannot replace the principal of its parent handle."""

    memory = Memory()
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    scope = memory.get_handle(root=root, principal="agent:worker")

    with pytest.raises(AccessDenied):
        scope.get_handle(principal="agent:admin")


def test_get_unbounded_handle_requires_explicit_roots_for_traversal() -> None:
    """Unbounded handles require explicit roots for tree traversal."""

    scope = Memory().get_unbounded_handle()

    with pytest.raises(AccessDenied):
        scope.children()
    with pytest.raises(AccessDenied):
        scope.ancestors()


def test_unbounded_handle_lists_links_for_explicit_blocks() -> None:
    """An unbounded handle lists the links of an explicit block and does not add a synthetic root."""

    memory = Memory()
    source = memory.put(type="node", value="source")
    target = memory.put(type="node", value="target")
    link = memory.add_link(source, target, "related")
    scope = memory.get_unbounded_handle()

    assert scope.list_links(source) == [link]


def test_unbounded_child_handle_can_remain_explicitly_unbounded() -> None:
    """Trusted code can narrow capabilities and keep a child handle unbounded, with no invented root."""

    parent = Memory().get_unbounded_handle(allow_global=True)

    child = parent.get_handle(allow_global=True)

    assert child.root_id is None
    assert child.allow_global is True


def test_get_unbounded_handle_requires_explicit_root_or_global_permission_for_search() -> None:
    """An unbounded handle does not change a subtree search into a global search."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    inside = memory.put(parent=root, type="note", value="needle inside")
    outside = memory.put(type="note", value="needle outside")
    scope = memory.get_unbounded_handle()

    with pytest.raises(AccessDenied):
        scope.search("needle")
    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="needle", scope=Scope.SELF))
    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="needle", scope=Scope.SUBTREE))

    scoped_results = scope.search("needle", root=root)
    global_scope = memory.get_unbounded_handle(allow_global=True)
    global_results = global_scope.search("needle", scope="global")

    assert [result.block.id for result in scoped_results] == [inside.id]
    assert {result.block.id for result in global_results} == {inside.id, outside.id}


def test_get_handle_requires_root_and_get_unbounded_handle_is_explicit() -> None:
    """`Memory.get_handle` creates only bounded handles, and unbounded access uses `get_unbounded_handle`."""

    memory = Memory()
    missing_attribute = "scope"

    with pytest.raises(AttributeError):
        getattr(memory, missing_attribute)
    with pytest.raises(TypeError):
        cast(Any, memory.get_handle)()
    with pytest.raises(ValueError):
        memory.get_handle(root=cast(Any, None))

    scope = memory.get_unbounded_handle(principal="agent:admin", allow_global=True)

    assert scope.root_id is None
    assert scope.principal.id == "agent:admin"
    assert scope.allow_global is True


def test_scope_root_must_exist() -> None:
    """`get_handle` raises `MemoryNotFound` if the root block does not exist."""

    memory = Memory()

    with pytest.raises(MemoryNotFound):
        memory.get_handle(root="missing")


def test_get_handle_requires_root_read_access_for_requested_principal() -> None:
    """A principal cannot receive a rooted handle for an unreadable root."""

    memory = Memory(principal="owner")
    private_root = memory.put(type="root", value="private")
    shared_root = memory.put(type="root", value="shared", policy=MemoryPolicy.public())

    with pytest.raises(AccessDenied):
        memory.get_handle(root=private_root, principal="agent:blocked")

    allowed = memory.get_handle(root=shared_root, principal="agent:allowed")

    assert allowed.root_id == shared_root.id
    assert allowed.principal.id == "agent:allowed"


def test_handle_set_and_update_need_read_on_the_block() -> None:
    """A handle principal that can write but not read a block gets `AccessDenied` and does not get the stored block."""

    owner = Memory(principal="owner")
    inbox = owner.put(type="inbox", value="inbox", policy=MemoryPolicy.public())
    drop_box = MemoryPolicy(read=AccessRule.owner(), write=AccessRule.allow())
    box = owner.put(parent=inbox, type="box", value={"secret": "owner only"}, metadata={"note": "private"}, policy=drop_box)
    agent = owner.get_handle(root=inbox, principal="agent")

    with pytest.raises(AccessDenied):
        agent.get(box)
    with pytest.raises(AccessDenied):
        agent.set(box, "overwritten")
    with pytest.raises(AccessDenied):
        agent.update(box, value={"hello": "hi"})
    dropped = agent.put(parent=inbox, type="box", value={"from": "agent"})

    assert owner.get(box) == box
    assert dropped.parent_id == inbox.id
    assert dropped.created_by == "agent"


def test_handle_require_access_checks_the_boundary_before_the_policy() -> None:
    """A handle rejects blocks outside its root first, then applies the block policy."""

    memory = Memory(principal="owner")
    readable = MemoryPolicy(read=AccessRule.allow())
    root = memory.put(type="root", value="root", policy=readable)
    inside = memory.put(parent=root, type="note", value="inside", policy=readable)
    outside = memory.put(type="note", value="outside", policy=MemoryPolicy.public())
    handle = memory.get_handle(root=root, principal="agent:reader")

    assert handle.require_access(Operation.READ, inside) is None
    with pytest.raises(AccessDenied, match="cannot 'write'"):
        handle.require_access(Operation.WRITE, inside)
    with pytest.raises(AccessDenied, match="outside memory handle root"):
        handle.require_access(Operation.READ, outside)
    with pytest.raises(AccessDenied, match="outside memory handle root"):
        handle.require_access(Operation.READ, "missing")
    with pytest.raises(MemoryNotFound):
        memory.get_unbounded_handle().require_access(Operation.READ, "missing")


def test_handle_contains_checks_the_root_against_the_boundary() -> None:
    """A handle refuses a `contains` root outside its boundary, and gives `False` for a candidate outside the root."""

    memory = Memory(principal="owner")
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    branch = memory.put(parent=root, type="branch", value="branch", policy=MemoryPolicy.public())
    leaf = memory.put(parent=branch, type="leaf", value="leaf", policy=MemoryPolicy.public())
    hidden = memory.put(parent=branch, type="leaf", value="hidden")
    sibling = memory.put(parent=root, type="branch", value="sibling", policy=MemoryPolicy.public())
    outside = memory.put(type="other", value="outside", policy=MemoryPolicy.public())
    handle = memory.get_handle(root=branch, principal="agent:reader")

    assert handle.contains(branch, leaf) is True
    assert handle.contains(leaf.id, leaf.id) is True
    assert handle.contains(branch, hidden) is False
    assert handle.contains(branch, sibling) is False
    assert handle.contains(branch, outside) is False
    assert handle.contains(branch, "missing") is False
    for refused in (root, sibling, outside, "missing"):
        with pytest.raises(AccessDenied, match="outside memory handle root"):
            handle.contains(refused, leaf)
    with pytest.raises(MemoryNotFound):
        memory.get_unbounded_handle().contains("missing", leaf)


def test_handle_delete_and_unbounded_ancestor_lookup_keep_explicit_boundaries() -> None:
    """A scoped handle can delete a block in its subtree, but an unbounded handle cannot infer an ancestor root."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")
    grandchild = memory.put(parent=child, type="grandchild", value="grandchild")

    memory.get_handle(root=root).delete(child)

    assert memory.get(child) is None
    assert memory.get(grandchild) is None
    with pytest.raises(AccessDenied, match="cannot infer which ancestors"):
        memory.get_unbounded_handle(allow_up=True).ancestors()


def test_handle_rejects_bad_block_references_before_boundary_checks() -> None:
    """A bad id raises `TypeError` or `ValueError`, not `AccessDenied` from the boundary check."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root, allow_mounts=True)
    calls = {
        "get": lambda ref: handle.get(ref),
        "set": lambda ref: handle.set(ref, "value"),
        "update": lambda ref: handle.update(ref, metadata={}),
        "delete": lambda ref: handle.delete(ref),
        "children": lambda ref: handle.children(ref),
        "contains root": lambda ref: handle.contains(ref, root),
        "contains block": lambda ref: handle.contains(root, ref),
        "list_links": lambda ref: handle.list_links(ref),
        "list_mounts": lambda ref: handle.list_mounts(ref),
        "discover_mounts": lambda ref: handle.discover_mounts(ref),
        "put parent": lambda ref: handle.put(type="note", value="value", parent=ref),
        "search": lambda ref: handle.search(root=ref),
        "get_handle": lambda ref: handle.get_handle(root=ref),
    }

    for call in calls.values():
        for ref in (123, {"$ne": None}):
            with pytest.raises(TypeError, match="MemoryBlock or a string id"):
                call(ref)
        with pytest.raises(ValueError, match="Block id must not be empty"):
            call("")
    with pytest.raises(TypeError, match="type must be a string"):
        handle.put(type=cast(str, 123), value="value")
