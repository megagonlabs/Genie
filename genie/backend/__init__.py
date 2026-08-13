"""Backend protocols and reference implementations."""

from genie.backend.base import MemoryBackend
from genie.backend.capabilities import BlockStore, LinkStore, SearchIndex, TraversalIndex
from genie.backend.composite import (
    InMemoryLinkStore,
    RouteInfo,
    RouteKey,
    RouteSelector,
    RoutedBackend,
)
from genie.backend.graph import Neo4jBackend
from genie.backend.in_memory import InMemoryBackend
from genie.backend.relational import SQLiteBackend
from genie.backend.structure_capabilities import (
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
)

__all__ = [
    "AcyclicLinkStore",
    "BlockStore",
    "InMemoryBackend",
    "InMemoryLinkStore",
    "KeyedStore",
    "LinkStore",
    "MemoryBackend",
    "Neo4jBackend",
    "RoutedBackend",
    "RouteInfo",
    "RouteKey",
    "RouteSelector",
    "SearchIndex",
    "SequenceStore",
    "SQLiteBackend",
    "TemporalIndex",
    "TemporalStore",
    "TraversalIndex",
]
