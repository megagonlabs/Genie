"""Backend protocols and reference implementations."""

from genie.backend.base import MemoryBackend
from genie.backend.capabilities import BlockStore, LinkStore, SearchIndex, TraversalIndex
from genie.backend.inmemory import InMemoryBackend
from genie.backend.structure_capabilities import (
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
)
from genie.backend.routed import (
    InMemoryLinkStore,
    RouteInfo,
    RouteKey,
    RouteSelector,
    RoutedBackend,
)

__all__ = [
    "AcyclicLinkStore",
    "BlockStore",
    "InMemoryBackend",
    "InMemoryLinkStore",
    "KeyedStore",
    "LinkStore",
    "MemoryBackend",
    "RoutedBackend",
    "RouteInfo",
    "RouteKey",
    "RouteSelector",
    "SearchIndex",
    "SequenceStore",
    "TemporalIndex",
    "TemporalStore",
    "TraversalIndex",
]
