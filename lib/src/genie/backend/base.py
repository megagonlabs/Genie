"""Top-level backend interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genie.backend.capabilities import BlockStore, LinkStore, SearchIndex, TraversalIndex


@runtime_checkable
class MemoryBackend(BlockStore, TraversalIndex, SearchIndex, LinkStore, Protocol):
    """Backend interface for Genie adapters.

    - **Forms:** a concrete backend can be a single database adapter, or a
      composition of store, search, and traversal implementations.
    - **Independence:** the backend layer keeps Genie independent of a specific
      database, storage layout, or retrieval strategy.
    """


@runtime_checkable
class BlockRouter(Protocol):
    """Capability for backends that route blocks to other backends.

    - **No state of its own:** a composite such as `RoutedBackend` owns no
      block, link, or structural state. It knows which backend owns a block. So
      callers and structure facades can reach the adapter that holds the
      records.
    - **No storage claim:** this protocol does not claim a storage capability. A
      router still advertises only the capabilities that it implements
      directly.
    """

    def backend_for_block(self, block_id: str) -> MemoryBackend | None:
        """Return the backend that owns a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Owning backend, or `None` when no route contains the id.
        """

        ...
