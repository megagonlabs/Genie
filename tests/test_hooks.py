from __future__ import annotations

from typing import Any

from genie import Memory, MemoryHook, MemoryQuery


def test_hooks_can_transform_and_observe_blocks() -> None:
    class SourceHook(MemoryHook):
        def __init__(self) -> None:
            self.created: list[str] = []

        def before_put(self, block):
            return block.with_updates(metadata={**block.metadata, "hooked": True})

        def after_put(self, block):
            self.created.append(block.id)

    hook = SourceHook()
    memory = Memory(hooks=[hook])

    block = memory.put(type="note", value="hello")

    assert block.metadata["hooked"] is True
    assert hook.created == [block.id]


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
