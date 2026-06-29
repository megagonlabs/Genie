from __future__ import annotations

from genie.hooks.base import MemoryHook
from genie.memory.block import MemoryBlock
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


class HookRegistry:
    def __init__(self, hooks: list[MemoryHook] | None = None) -> None:
        self._hooks = list(hooks or ())

    def register(self, hook: MemoryHook) -> None:
        self._hooks.append(hook)

    def before_put(self, block: MemoryBlock) -> MemoryBlock:
        for hook in self._hooks:
            block = hook.before_put(block)
        return block

    def after_put(self, block: MemoryBlock) -> None:
        for hook in self._hooks:
            hook.after_put(block)

    def before_get(self, block_id: str) -> None:
        for hook in self._hooks:
            hook.before_get(block_id)

    def after_get(self, block: MemoryBlock | None) -> None:
        for hook in self._hooks:
            hook.after_get(block)

    def before_search(self, query: MemoryQuery) -> MemoryQuery:
        for hook in self._hooks:
            query = hook.before_search(query)
        return query

    def after_search(
        self,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        for hook in self._hooks:
            results = hook.after_search(query, results)
        return results

    def before_delete(self, block: MemoryBlock) -> None:
        for hook in self._hooks:
            hook.before_delete(block)

    def after_delete(self, block: MemoryBlock) -> None:
        for hook in self._hooks:
            hook.after_delete(block)
