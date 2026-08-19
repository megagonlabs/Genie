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
