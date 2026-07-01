"""Backend protocols and reference implementations."""

from genie.backend.base import MemoryBackend
from genie.backend.capabilities import BlockStore, LinkStore, SearchIndex, TraversalIndex
from genie.backend.inmemory import InMemoryBackend
from genie.backend.routed import InMemoryLinkStore, RouteInfo, RouteKey, RouteSelector, RoutedBackend

__all__ = [
    "BlockStore",
    "InMemoryBackend",
    "InMemoryLinkStore",
    "LinkStore",
    "MemoryBackend",
    "RoutedBackend",
    "RouteInfo",
    "RouteKey",
    "RouteSelector",
    "SearchIndex",
    "TraversalIndex",
]
