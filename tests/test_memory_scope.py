from __future__ import annotations

import pytest

from genie import AccessDenied, Memory, MemoryQuery, Scope


def test_subtree_scope_limits_search_scope() -> None:
    memory = Memory()
    root = memory.put(type="root", value="workspace")
    task = memory.put(parent=root, type="task", value="launch task")
    memory.put(parent=task, type="note", value="subtree only clue")
    memory.put(type="note", value="global only clue")

    scope = memory.scope(root=task)

    assert [result.block.value for result in scope.search("clue")] == ["subtree only clue"]
    with pytest.raises(AccessDenied):
        scope.search("clue", scope="global")


def test_scope_can_optionally_search_ancestors() -> None:
    memory = Memory()
    root = memory.put(type="root", value="project launch context")
    task = memory.put(parent=root, type="task", value="implementation task")

    restricted = memory.scope(root=task)
    with pytest.raises(AccessDenied):
        restricted.search("project", scope="ancestors")

    allowed = memory.scope(root=task, allow_up=True)
    results = allowed.search("project", scope="ancestors")

    assert [result.block.id for result in results] == [root.id]


def test_subscopes_can_narrow_but_not_escape_parent_scope() -> None:
    memory = Memory()
    project = memory.put(type="project", value="project")
    task = memory.put(parent=project, type="task", value="task")
    step = memory.put(parent=task, type="step", value="step")
    sibling = memory.put(parent=project, type="task", value="sibling")
    outside = memory.put(type="project", value="outside")

    project_scope = memory.scope(root=project)
    task_scope = project_scope.scope(root=task)
    child = task_scope.put(type="observation", value="scoped child")

    assert child.parent_id == task.id
    retrieved = task_scope.get(step)
    assert retrieved is not None
    assert retrieved.value == "step"

    with pytest.raises(AccessDenied):
        task_scope.get(sibling)
    with pytest.raises(AccessDenied):
        project_scope.scope(root=outside)


def test_subscopes_cannot_widen_parent_global_or_ancestor_access() -> None:
    memory = Memory()
    root = memory.put(type="root", value="root context")
    task = memory.put(parent=root, type="task", value="task")
    memory.put(type="note", value="global clue")

    restricted = memory.scope(root=task)

    with pytest.raises(AccessDenied):
        restricted.scope(allow_up=True)
    with pytest.raises(AccessDenied):
        restricted.scope(allow_global=True)

    privileged = memory.scope(root=task, allow_up=True, allow_global=True)
    subscope = privileged.scope(allow_up=True, allow_global=True)

    assert [result.block.value for result in subscope.search("root", scope="ancestors")] == [
        "root context"
    ]
    assert [result.block.value for result in subscope.search("global", scope="global")] == [
        "global clue"
    ]


def test_scope_search_cannot_escape_through_memory_query_objects() -> None:
    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")
    outside = memory.put(type="note", value="outside needle")
    scope = memory.scope(root=root)

    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="outside", root_id=outside.id, scope=Scope.SELF))
    with pytest.raises(AccessDenied):
        scope.search(MemoryQuery(text="outside", scope=Scope.GLOBAL))

    upward_scope = memory.scope(root=child, allow_up=True)
    with pytest.raises(AccessDenied):
        upward_scope.search(MemoryQuery(text="outside", root_id=outside.id, scope=Scope.ANCESTORS))

    global_scope = memory.scope(root=root, allow_global=True)
    results = global_scope.search(MemoryQuery(text="outside", scope=Scope.GLOBAL))

    assert [result.block.id for result in results] == [outside.id]


def test_subscopes_cannot_change_principal_identity() -> None:
    memory = Memory()
    root = memory.put(type="root", value="root")
    scope = memory.scope(root=root, principal="agent:worker")

    with pytest.raises(AccessDenied):
        scope.scope(principal="agent:admin")


def test_rootless_scope_requires_explicit_roots_for_traversal() -> None:
    scope = Memory().scope(root=None)

    with pytest.raises(AccessDenied):
        scope.children()
    with pytest.raises(AccessDenied):
        scope.ancestors()
