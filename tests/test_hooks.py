"""Tests for memory hook dispatch and customization."""

from __future__ import annotations

from typing import Any

from genie import HookRegistry, Memory, MemoryHook, MemoryQuery


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


def test_get_and_delete_hooks_observe_missing_and_deleted_blocks() -> None:
    """Lifecycle hooks observe missing gets and successful deletes."""

    class LifecycleHook(MemoryHook):
        """Hook that records get and delete lifecycle events."""

        def __init__(self) -> None:
            self.before_get_calls: list[str] = []
            self.after_get_calls: list[str | None] = []
            self.deleted: list[str] = []

        def before_get(self, block_id: str) -> None:
            """Record ids before lookup."""

            self.before_get_calls.append(block_id)

        def after_get(self, block: Any) -> None:
            """Record looked-up block ids or missing values."""

            self.after_get_calls.append(block.id if block is not None else None)

        def after_delete(self, block: Any) -> None:
            """Record deleted block ids."""

            self.deleted.append(block.id)

    hook = LifecycleHook()
    memory = Memory(hooks=[hook])
    block = memory.put(type="note", value="note")

    assert memory.get("missing") is None
    memory.delete(block)

    assert hook.before_get_calls == ["missing"]
    assert hook.after_get_calls == [None]
    assert hook.deleted == [block.id]


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
