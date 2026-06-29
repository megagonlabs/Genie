from __future__ import annotations

from genie.memory.block import MemoryBlock
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


class MemoryHook:
    """No-op hook base class for extending Genie behavior."""

    def before_put(self, block: MemoryBlock) -> MemoryBlock:
        return block

    def after_put(self, block: MemoryBlock) -> None:
        return None

    def before_get(self, block_id: str) -> None:
        return None

    def after_get(self, block: MemoryBlock | None) -> None:
        return None

    def before_search(self, query: MemoryQuery) -> MemoryQuery:
        return query

    def after_search(
        self,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        return results

    def before_delete(self, block: MemoryBlock) -> None:
        return None

    def after_delete(self, block: MemoryBlock) -> None:
        return None
