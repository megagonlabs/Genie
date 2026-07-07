"""Tests for memory hook dispatch and customization."""

from __future__ import annotations

from typing import Any

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    HookRegistry,
    Memory,
    MemoryHook,
    MemoryPolicy,
    MemoryQuery,
    Scope,
    SearchResult,
)


def test_hooks_can_transform_and_observe_blocks() -> None:
    """Put hooks can transform blocks and observe stored ids."""

    class SourceHook(MemoryHook):
        """Hook that annotates created blocks and records their ids."""

        def __init__(self) -> None:
            self.created: list[str] = []

        def before_put(self, block):
            """Add marker metadata before storage."""

            return block.with_updates(metadata={**block.metadata, "hooked": True})

        def after_put(self, block):
            """Record the stored block id."""

            self.created.append(block.id)

    hook = SourceHook()
    memory = Memory(hooks=[hook])

    block = memory.put(type="note", value="hello")

    assert block.metadata["hooked"] is True
    assert hook.created == [block.id]


def test_before_put_hook_cannot_change_parent_after_authorization() -> None:
    """Put hooks cannot reparent a block after scope and policy checks."""

    class ReparentHook(MemoryHook):
        """Hook that tries to move new blocks to another parent."""

        def __init__(self, parent_id: str) -> None:
            self.parent_id = parent_id

        def before_put(self, block):
            """Attempt to change the authorized parent."""

            return block.with_updates(parent_id=self.parent_id)

    memory = Memory()
    root = memory.put(type="root", value="root")
    outside = memory.put(type="root", value="outside")
    memory.hooks.register(ReparentHook(outside.id))
    scope = memory.get_handle(root=root)

    with pytest.raises(ValueError, match="parent_id"):
        scope.put(type="note", value="should not be stored")

    assert memory.children(root) == []
    assert memory.children(outside) == []


def test_search_hooks_can_rewrite_and_trim_results() -> None:
    """Search hooks can rewrite queries and trim result lists."""

    class SearchHook(MemoryHook):
        """Hook that rewrites the query and trims results."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def before_search(self, query: MemoryQuery) -> MemoryQuery:
            """Replace the incoming text query with a known needle."""

            self.calls.append(("before", query.text))
            return MemoryQuery(text="needle", limit=query.limit)

        def after_search(self, query: MemoryQuery, results: list[Any]) -> list[Any]:
            """Keep only the first result."""

            self.calls.append(("after", query.text))
            return results[:1]

    hook = SearchHook()
    memory = Memory(hooks=[hook])
    first = memory.put(type="note", value="needle one")
    memory.put(type="note", value="needle two")

    results = memory.search("ignored")

    assert [result.block.id for result in results] == [first.id]
    assert hook.calls == [("before", "ignored"), ("after", "needle")]


def test_before_search_hook_cannot_widen_query_after_authorization() -> None:
    """Search hooks cannot switch a query to an unauthorized root after checks."""

    class EscalatingHook(MemoryHook):
        """Hook that tries to redirect search into a private subtree."""

        def __init__(self, private_root_id: str) -> None:
            self.private_root_id = private_root_id

        def before_search(self, query: MemoryQuery) -> MemoryQuery:
            """Replace the authorized query with a private subtree query."""

            return MemoryQuery(
                text="secret",
                root_id=self.private_root_id,
                scope=Scope.SUBTREE,
                limit=query.limit,
            )

    owner = Memory(principal="owner")
    public_root = owner.put(type="root", value="public")
    private_root = owner.put(
        type="root",
        value="private",
        policy=MemoryPolicy(
            search=AccessRule.only(principals={"owner"}),
            traverse_down=AccessRule.only(principals={"owner"}),
        ),
    )
    owner.put(parent=private_root, type="note", value="secret")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[EscalatingHook(private_root.id)],
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        reader.search("public", root=public_root, scope="subtree")


def test_after_search_hook_results_are_rechecked_for_policy_and_scope() -> None:
    """Search results returned by hooks stay inside policy and query scope."""

    class InjectingHook(MemoryHook):
        """Hook that tries to add unreadable and out-of-scope results."""

        def __init__(self, injected: list[SearchResult]) -> None:
            self.injected = injected

        def after_search(
            self,
            query: MemoryQuery,
            results: list[SearchResult],
        ) -> list[SearchResult]:
            """Append injected results after normal policy filtering."""

            return [*results, *self.injected]

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    inside = owner.put(parent=root, type="note", value="needle inside")
    private_inside = owner.put(
        parent=root,
        type="note",
        value="needle private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    outside = owner.put(type="note", value="needle outside")
    injected = [
        SearchResult(block=private_inside, score=99, reason="injected private"),
        SearchResult(block=outside, score=98, reason="injected outside"),
    ]
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[InjectingHook(injected)],
        codecs=owner.codecs,
    )

    results = reader.search("needle", root=root, scope="subtree")

    assert [result.block.id for result in results] == [inside.id]


def test_get_and_delete_hooks_observe_missing_and_deleted_blocks() -> None:
    """Hooks observe missing gets and every deleted subtree block."""

    class LifecycleHook(MemoryHook):
        """Hook that records get and delete events."""

        def __init__(self) -> None:
            self.before_get_calls: list[str] = []
            self.after_get_calls: list[str | None] = []
            self.before_deleted: list[str] = []
            self.deleted: list[str] = []

        def before_get(self, block_id: str) -> None:
            """Record ids before lookup."""

            self.before_get_calls.append(block_id)

        def after_get(self, block: Any) -> None:
            """Record looked-up block ids or missing values."""

            self.after_get_calls.append(block.id if block is not None else None)

        def before_delete(self, block: Any) -> None:
            """Record ids before deletion."""

            self.before_deleted.append(block.id)

        def after_delete(self, block: Any) -> None:
            """Record deleted block ids."""

            self.deleted.append(block.id)

    hook = LifecycleHook()
    memory = Memory(hooks=[hook])
    block = memory.put(type="note", value="note")
    child = memory.put(parent=block, type="note", value="child")

    assert memory.get("missing") is None
    memory.delete(block)

    assert hook.before_get_calls == ["missing"]
    assert hook.after_get_calls == [None]
    assert hook.before_deleted == [block.id, child.id]
    assert hook.deleted == [block.id, child.id]


def test_hook_registry_runs_hooks_in_registration_order() -> None:
    """HookRegistry applies transformations in deterministic order."""

    class OrderedHook(MemoryHook):
        """Hook that appends a marker to block metadata."""

        def __init__(self, marker: str, calls: list[str]) -> None:
            self.marker = marker
            self.calls = calls

        def before_put(self, block):
            """Append the marker to the ordered metadata list."""

            self.calls.append(self.marker)
            markers = [*block.metadata.get("markers", ()), self.marker]
            return block.with_updates(metadata={**block.metadata, "markers": markers})

    calls: list[str] = []
    registry = HookRegistry([OrderedHook("first", calls)])
    registry.register(OrderedHook("second", calls))
    memory = Memory(hooks=registry)

    block = memory.put(type="note", value="ordered")

    assert calls == ["first", "second"]
    assert block.metadata["markers"] == ["first", "second"]
