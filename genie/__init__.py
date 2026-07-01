"""Genie public API.

Genie provides database-agnostic primitives for hierarchical, scoped, and
policy-aware agent memory. Most applications start with `Memory` and pass
`MemoryScope` instances to agents.
"""

from genie.backend import (
    InMemoryBackend,
    InMemoryLinkStore,
    MemoryBackend,
    RouteInfo,
    RouteKey,
    RouteSelector,
    RoutedBackend,
)
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec, MemoryCodec
from genie.hooks import HookRegistry, MemoryHook
from genie.memory.block import MemoryBlock
from genie.memory.errors import GenieError, MemoryConflict, MemoryNotFound, UnsupportedOperation
from genie.memory.links import LinkRelations, MemoryLink
from genie.memory.memory import Memory
from genie.memory.scope import MemoryScope
from genie.policy.engine import AccessDenied, Operation, PolicyEngine
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

__all__ = [
    "AccessDenied",
    "AccessRule",
    "CodecRegistry",
    "EncodedValue",
    "GenieError",
    "HookRegistry",
    "InMemoryBackend",
    "InMemoryLinkStore",
    "JsonCodec",
    "LinkRelations",
    "Memory",
    "MemoryBackend",
    "MemoryBlock",
    "MemoryConflict",
    "MemoryCodec",
    "MemoryHook",
    "MemoryLink",
    "MemoryNotFound",
    "MemoryPolicy",
    "MemoryQuery",
    "MemoryScope",
    "Operation",
    "Principal",
    "PolicyEngine",
    "RoutedBackend",
    "RouteInfo",
    "RouteKey",
    "RouteSelector",
    "Scope",
    "SearchResult",
    "UnsupportedOperation",
]
