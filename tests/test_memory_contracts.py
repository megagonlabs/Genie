from __future__ import annotations

import importlib
import math
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    EncodedValue,
    InMemoryBackend,
    Memory,
    MemoryHook,
    MemoryPolicy,
    MemoryQuery,
    MemoryScope,
    Principal,
    Scope,
    SearchResult,
)
from genie.codecs import CodecRegistry
from genie.memory.errors import MemoryConflict, MemoryNotFound


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

    assert [result.block.value for result in subscope.search("root", scope="ancestors")] == ["root context"]
    assert [result.block.value for result in subscope.search("global", scope="global")] == ["global clue"]


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


def test_search_filters_types_scopes_and_limits() -> None:
    memory = Memory()
    root = memory.put(type="root", value="workspace")
    high = memory.put(
        parent=root,
        type="note",
        value="alpha alpha",
        metadata={"priority": 3, "owner": "team"},
    )
    memory.put(
        parent=root,
        type="note",
        value="alpha",
        metadata={"priority": 1, "owner": "team"},
    )
    memory.put(
        parent=root,
        type="decision",
        value="alpha",
        metadata={"priority": 5, "owner": "team"},
    )

    results = memory.search(
        "alpha",
        root=root,
        scope="subtree",
        types=["note"],
        filters={"metadata.priority": {">=": 2}, "owner": "team"},
        limit=1,
    )

    assert [result.block.id for result in results] == [high.id]
    assert results[0].score == 2.0
    assert [result.block.id for result in memory.search(root=high, scope="self")] == [high.id]


def test_filter_operators_cover_common_comparisons() -> None:
    memory = Memory()
    memory.put(type="note", value="low", metadata={"priority": 1, "owner": "team"})
    mid = memory.put(type="note", value="mid", metadata={"priority": 2, "owner": "team"})
    memory.put(type="note", value="high", metadata={"priority": 3, "owner": "other"})

    results = memory.search(
        types=["note"],
        filters={
            "metadata.priority": {">": 1, "<=": 2},
            "owner": {"in": ["team"], "!=": "other"},
        },
    )

    assert [result.block.id for result in results] == [mid.id]
    assert memory.search(filters={"metadata.priority": {"$unknown": 2}}) == []


def test_canonical_filter_operators_still_work() -> None:
    memory = Memory()
    block = memory.put(type="note", value="canonical", metadata={"priority": 2})

    results = memory.search(filters={"metadata.priority": {"$gte": 2, "$lte": 2}})

    assert [result.block.id for result in results] == [block.id]


def test_backends_receive_normalized_filter_operators() -> None:
    class RecordingBackend(InMemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.seen_filters: dict[str, Any] | None = None

        def search(self, query: MemoryQuery) -> list[SearchResult]:
            self.seen_filters = query.filters
            return super().search(query)

    backend = RecordingBackend()
    memory = Memory(backend=backend)
    memory.put(type="note", value="normalized", metadata={"priority": 2})

    results = memory.search(filters={"metadata.priority": {">=": 2, "<=": 3}})

    assert [result.block.value for result in results] == ["normalized"]
    assert backend.seen_filters == {"metadata.priority": {"$gte": 2, "$lte": 3}}


def test_links_support_direction_filters_and_link_scope_search() -> None:
    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="linked needle")
    inbound = memory.put(type="note", value="inbound")

    related = memory.link(source, target, "related", metadata={"weight": 0.8})
    incoming = memory.link(inbound, source, "mentions")

    assert memory.links(source, relation="related") == [related]
    assert memory.links(source, direction="in") == [incoming]
    assert sorted(link.id for link in memory.links(source, direction="both")) == sorted([related.id, incoming.id])

    results = memory.search("needle", root=source, scope="links")

    assert [result.block.id for result in results] == [target.id]


def test_links_reject_invalid_directions() -> None:
    memory = Memory()
    block = memory.put(type="note", value="source")

    with pytest.raises(ValueError):
        memory.links(block, direction=cast(Any, "sideways"))


def test_scope_linking_cannot_cross_scope_boundaries() -> None:
    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="note", value="child")
    outside = memory.put(type="note", value="outside")
    scope = memory.scope(root=root)

    with pytest.raises(AccessDenied):
        scope.link(child, outside, "related")


def test_set_delete_and_recursive_cleanup() -> None:
    memory = Memory()
    root = memory.put(type="root", value="old")
    child = memory.put(parent=root, type="child", value="child")
    grandchild = memory.put(parent=child, type="grandchild", value="grandchild")
    memory.link(root, child, "contains")

    updated = memory.set(root, "new")
    assert updated.value == "new"
    assert updated.version == 2

    memory.delete(child)

    assert memory.get(child.id) is None
    assert memory.get(grandchild.id) is None
    assert memory.children(root) == []
    assert memory.links(root) == []


def test_delete_requires_write_access_for_descendants() -> None:
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
    memory = Memory()
    block = memory.put(type="note", value="exists", id="fixed")

    with pytest.raises(MemoryNotFound):
        memory.delete("missing")
    with pytest.raises(MemoryConflict):
        memory.put(type="note", value="duplicate", id=block.id)
    with pytest.raises(MemoryNotFound):
        memory.put(type="note", value="orphan", parent="missing")


def test_patch_requires_dict_values() -> None:
    memory = Memory()
    block = memory.put(type="note", value="plain text")

    with pytest.raises(TypeError):
        memory.patch(block, value={"field": "value"})


def test_search_hooks_can_rewrite_and_trim_results() -> None:
    class SearchHook(MemoryHook):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def before_search(self, query: MemoryQuery) -> MemoryQuery:
            self.calls.append(("before", query.text))
            return MemoryQuery(text="needle", limit=query.limit)

        def after_search(self, query: MemoryQuery, results: list[Any]) -> list[Any]:
            self.calls.append(("after", query.text))
            return results[:1]

    hook = SearchHook()
    memory = Memory(hooks=[hook])
    first = memory.put(type="note", value="needle one")
    memory.put(type="note", value="needle two")

    results = memory.search("ignored")

    assert [result.block.id for result in results] == [first.id]
    assert hook.calls == [("before", "ignored"), ("after", "needle")]


def test_get_and_delete_hooks_observe_missing_and_deleted_blocks() -> None:
    class LifecycleHook(MemoryHook):
        def __init__(self) -> None:
            self.before_get_calls: list[str] = []
            self.after_get_calls: list[str | None] = []
            self.deleted: list[str] = []

        def before_get(self, block_id: str) -> None:
            self.before_get_calls.append(block_id)

        def after_get(self, block: Any) -> None:
            self.after_get_calls.append(block.id if block is not None else None)

        def after_delete(self, block: Any) -> None:
            self.deleted.append(block.id)

    hook = LifecycleHook()
    memory = Memory(hooks=[hook])
    block = memory.put(type="note", value="note")

    assert memory.get("missing") is None
    memory.delete(block)

    assert hook.before_get_calls == ["missing"]
    assert hook.after_get_calls == [None]
    assert hook.deleted == [block.id]


def test_policy_roles_allow_access_without_principal_id_match() -> None:
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


def test_codec_registry_reports_unknown_encoders_and_decoders() -> None:
    registry = CodecRegistry()

    with pytest.raises(TypeError):
        registry.encode(object())
    with pytest.raises(TypeError):
        registry.decode(EncodedValue("application/vnd.unknown", {}))


def test_codec_registry_reports_unknown_requested_content_type() -> None:
    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="note", value={"safe": True}, content_type="application/vnd.missing")


def test_json_codec_rejects_non_json_compatible_values() -> None:
    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="bad", value={"items": {1, 2, 3}})
    with pytest.raises(TypeError):
        memory.put(type="bad", value={"score": math.nan})


def test_query_accepts_tuple_types_and_string_scope() -> None:
    query = MemoryQuery(scope="subtree", types=("note",))

    assert query.scope == Scope.SUBTREE
    assert query.types == ("note",)


def test_backend_parent_moves_update_children_indexes() -> None:
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    old_parent = memory.put(type="root", value="old")
    new_parent = memory.put(type="root", value="new")
    child = memory.put(parent=old_parent, type="child", value="child")

    raw_child = backend.get_block(child.id)
    assert raw_child is not None
    backend.update_block(raw_child.with_updates(parent_id=new_parent.id))

    assert backend.children(old_parent.id) == []
    assert [block.id for block in backend.children(new_parent.id)] == [child.id]


def test_backend_rejects_reparenting_cycles() -> None:
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")

    raw_root = backend.get_block(root.id)
    raw_child = backend.get_block(child.id)
    assert raw_root is not None
    assert raw_child is not None

    with pytest.raises(MemoryConflict):
        backend.update_block(raw_root.with_updates(parent_id=child.id))
    with pytest.raises(MemoryConflict):
        backend.update_block(raw_child.with_updates(parent_id=child.id))


def test_returned_blocks_do_not_mutate_stored_values_or_metadata() -> None:
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


def test_filter_incompatible_comparisons_fail_closed() -> None:
    memory = Memory()
    memory.put(type="note", value="note", metadata={"priority": "high"})

    assert memory.search(filters={"metadata.priority": {">": 1}}) == []
