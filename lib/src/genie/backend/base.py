"""Top-level backend interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genie.backend.capabilities import BlockStore, LinkStore, SearchIndex, TraversalIndex


@runtime_checkable
class MemoryBackend(BlockStore, TraversalIndex, SearchIndex, LinkStore, Protocol):
    """Backend interface for Genie adapters.

    A concrete backend can be a single database adapter or a composition of
    store/search/traversal implementations. The backend layer keeps Genie
    independent of any specific database, storage layout, or retrieval
    strategy.
    """


@runtime_checkable
class BlockRouter(Protocol):
    """Capability for backends that route blocks to other backends.

    A composite such as `RoutedBackend` owns no block, link, or structural state
    of its own. It knows which backend owns a block, which is what lets callers
    and structure facades reach the adapter that actually holds the records.

    Implementing this protocol is not a claim to any storage capability. A router
    still advertises only the capabilities it implements directly.
    """

    def backend_for_block(self, block_id: str) -> MemoryBackend | None:
        """Return the backend that owns a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Owning backend, or `None` when no route contains the id.
        """

        ...
