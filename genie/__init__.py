"""Genie public API.

Genie provides platform-agnostic, storage/database-agnostic, and
use-case-agnostic building blocks for developers building their own memory
systems. The core package defines memory blocks, parent/child ownership, scoped
access, owner-private default policies, and interfaces for storage, search,
links, policies, hooks, and value encoding. Applications provide their own
business logic, memory structure, storage adapters, retrieval strategy, and
value types.

Most applications start with `Memory` and pass bounded `MemoryHandle` instances
to agents.
"""

from genie.backend import (
    AcyclicLinkStore,
    InMemoryBackend,
    InMemoryLinkStore,
    KeyedStore,
    MemoryBackend,
    RouteInfo,
    RouteKey,
    RouteSelector,
    RoutedBackend,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
)
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec, MemoryCodec
from genie.hooks import HookRegistry, MemoryHook
from genie.memory.block import MemoryBlock
from genie.memory.errors import GenieError, MemoryConflict, MemoryNotFound, UnsupportedOperation
from genie.memory.links import LinkRelations, MemoryLink
from genie.memory.memory import Memory
from genie.memory.handle import MemoryHandle
from genie.policy.engine import AccessDenied, Operation, PolicyEngine
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

__all__ = [
    "AcyclicLinkStore",
    "AccessDenied",
    "AccessRule",
    "CodecRegistry",
    "EncodedValue",
    "GenieError",
    "HookRegistry",
    "InMemoryBackend",
    "InMemoryLinkStore",
    "JsonCodec",
    "KeyedStore",
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
    "MemoryHandle",
    "Operation",
    "Principal",
    "PolicyEngine",
    "RoutedBackend",
    "RouteInfo",
    "RouteKey",
    "RouteSelector",
    "Scope",
    "SearchResult",
    "SequenceStore",
    "TemporalIndex",
    "TemporalStore",
    "UnsupportedOperation",
]
