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
    """Ancestor search is available only when explicitly enabled."""

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
    """Handles use the memory default policy unless policy override is enabled."""

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
    """Trusted handles can attach the same policy object as `Memory.put`."""

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


def test_scope_search_cannot_escape_through_memory_query_objects() -> None:
    """Prebuilt queries cannot pass wider roots through a scope."""

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
    """Child handles cannot replace the principal they were created with."""

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


def test_get_unbounded_handle_requires_explicit_root_or_global_permission_for_search() -> None:
    """Unbounded handles cannot accidentally turn subtree search into global search."""

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
    """Memory.get_handle creates bounded handles; unbounded access uses get_unbounded_handle."""

    memory = Memory()

    with pytest.raises(AttributeError):
        getattr(memory, "scope")
    with pytest.raises(TypeError):
        cast(Any, memory.get_handle)()
    with pytest.raises(ValueError):
        memory.get_handle(root=cast(Any, None))

    scope = memory.get_unbounded_handle(principal="agent:admin", allow_global=True)

    assert scope.root_id is None
    assert scope.principal.id == "agent:admin"
    assert scope.allow_global is True


def test_scope_root_must_exist() -> None:
    """Bounded handles fail early when their root block is missing."""

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
