"""Hook dispatch registry."""

from __future__ import annotations

from genie.hooks.base import MemoryHook
from genie.memory.block import MemoryBlock
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


class HookRegistry:
    """Ordered collection of memory hooks."""

    def __init__(self, hooks: list[MemoryHook] | None = None) -> None:
        """Create a registry.

        Args:
            hooks: Initial hooks in dispatch order.
        """

        self._hooks = list(hooks or ())

    def register(self, hook: MemoryHook) -> None:
        """Append a hook to the dispatch order.

        Args:
            hook: Hook to register.
        """

        self._hooks.append(hook)

    def before_put(self, block: MemoryBlock) -> MemoryBlock:
        """Dispatch `before_put` to each hook in order.

        Args:
            block: Block being created.

        Returns:
            Block returned by the final hook.
        """

        for hook in self._hooks:
            block = hook.before_put(block)
        return block

    def after_put(self, block: MemoryBlock) -> None:
        """Dispatch `after_put` to each hook in order.

        Args:
            block: Stored block.
        """

        for hook in self._hooks:
            hook.after_put(block)

    def before_get(self, block_id: str) -> None:
        """Dispatch `before_get` to each hook in order.

        Args:
            block_id: Id being fetched.
        """

        for hook in self._hooks:
            hook.before_get(block_id)

    def after_get(self, block: MemoryBlock | None) -> None:
        """Dispatch `after_get` to each hook in order.

        Args:
            block: Fetched block, or `None`.
        """

        for hook in self._hooks:
            hook.after_get(block)

    def before_search(self, query: MemoryQuery) -> MemoryQuery:
        """Dispatch `before_search` to each hook in order.

        Args:
            query: Query about to be executed.

        Returns:
            Query returned by the final hook.
        """

        for hook in self._hooks:
            query = hook.before_search(query)
        return query

    def after_search(
        self,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Dispatch `after_search` to each hook in order.

        Args:
            query: Query that was executed.
            results: Search results.

        Returns:
            Results returned by the final hook.
        """

        for hook in self._hooks:
            results = hook.after_search(query, results)
        return results

    def before_delete(self, block: MemoryBlock) -> None:
        """Dispatch `before_delete` to each hook in order.

        Args:
            block: Block about to be deleted.
        """

        for hook in self._hooks:
            hook.before_delete(block)

    def after_delete(self, block: MemoryBlock) -> None:
        """Dispatch `after_delete` to each hook in order.

        Args:
            block: Deleted block.
        """

        for hook in self._hooks:
            hook.after_delete(block)
