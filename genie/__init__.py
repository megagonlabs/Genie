"""Genie public API.

Genie provides database-agnostic primitives for hierarchical, scoped, and
policy-aware agent memory. Most applications start with `Memory` and pass
`MemoryScope` instances to agents.
"""

from genie.backend import InMemoryBackend, MemoryBackend
from genie.codecs import CodecRegistry, EncodedValue, JsonCodec, MemoryCodec
from genie.hooks import HookRegistry, MemoryHook
from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.memory.memory import Memory
from genie.memory.scope import MemoryScope
from genie.policy.engine import AccessDenied
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.policy.principal import Principal
from genie.query import MemoryQuery, Scope, SearchResult

__all__ = [
    "AccessDenied",
    "AccessRule",
    "CodecRegistry",
    "EncodedValue",
    "HookRegistry",
    "InMemoryBackend",
    "JsonCodec",
    "Memory",
    "MemoryBackend",
    "MemoryBlock",
    "MemoryCodec",
    "MemoryHook",
    "MemoryLink",
    "MemoryPolicy",
    "MemoryQuery",
    "MemoryScope",
    "Principal",
    "Scope",
    "SearchResult",
]
