"""Hook interface for observing or customizing memory operations."""

from __future__ import annotations

from genie.memory.block import MemoryBlock
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


class MemoryHook:
    """No-op hook base class for customizing Genie behavior.

    Subclasses can override one or more methods. Hooks run inside the public
    `Memory` operation flow, so they should avoid long-running or unreliable
    side effects unless the application is prepared for those failures.
    """

    def before_put(self, block: MemoryBlock) -> MemoryBlock:
        """Run before a block is stored.

        Hooks may annotate or rewrite fields, but they must not change
        `parent_id`; parent placement is checked before hooks run.

        Args:
            block: Block about to be stored.

        Returns:
            Block to pass to the backend. Hooks may return a modified copy.
        """

        return block

    def after_put(self, block: MemoryBlock) -> None:
        """Run after a block is stored.

        Args:
            block: Stored block.
        """

        return None

    def before_get(self, block_id: str) -> None:
        """Run before a block lookup.

        Args:
            block_id: Id being fetched.
        """

        return None

    def after_get(self, block: MemoryBlock | None) -> None:
        """Run after a block lookup.

        Args:
            block: Fetched block, or `None` when missing.
        """

        return None

    def before_search(self, query: MemoryQuery) -> MemoryQuery:
        """Run before search is sent to the backend.

        Args:
            query: Normalized memory query.

        Returns:
            Query to send to the backend. Hooks may return a modified query.
        """

        return query

    def after_search(
        self,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Run after search results are decoded and policy-filtered.

        Args:
            query: Query that was executed.
            results: Search results.

        Returns:
            Results to return to the caller. Hooks may rerank or filter them.
        """

        return results

    def before_delete(self, block: MemoryBlock) -> None:
        """Run before a block is deleted.

        Args:
            block: Block about to be deleted.
        """

        return None

    def after_delete(self, block: MemoryBlock) -> None:
        """Run after a block is deleted.

        Args:
            block: Deleted block.
        """

        return None
