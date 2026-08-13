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
    Neo4jBackend,
    RouteInfo,
    RouteKey,
    RouteSelector,
    RoutedBackend,
    SequenceStore,
    SQLiteBackend,
    TemporalIndex,
    TemporalStore,
)
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec, MemoryCodec
from genie.hooks import (
    HookContext,
    HookOperation,
    HookOrigin,
    HookRegistry,
    MemoryHook,
    StructureAction,
    StructureChange,
    StructureKind,
)
from genie.memory.block import MemoryBlock
from genie.memory.errors import (
    BackendBusy,
    BackendCapacityExceeded,
    BackendCorruption,
    BackendError,
    BackendReadOnly,
    BackendUnavailable,
    GenieError,
    HookRecursionError,
    MemoryConflict,
    MemoryNotFound,
    UnsupportedOperation,
)
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
    "BackendBusy",
    "BackendCapacityExceeded",
    "BackendCorruption",
    "BackendError",
    "BackendReadOnly",
    "BackendUnavailable",
    "CodecRegistry",
    "EncodedValue",
    "GenieError",
    "HookContext",
    "HookOperation",
    "HookOrigin",
    "HookRecursionError",
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
    "Neo4jBackend",
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
    "SQLiteBackend",
    "StructureAction",
    "StructureChange",
    "StructureKind",
    "TemporalIndex",
    "TemporalStore",
    "UnsupportedOperation",
]
