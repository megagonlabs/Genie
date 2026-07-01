# Genie

Genie is a database-agnostic foundation for hierarchical agent memory.

For a deeper architecture overview, see [docs/design.md](docs/design.md).

The core package defines the memory structure and extension contracts:

- `MemoryBlock`: the smallest independently addressable unit of memory.
- `Memory`: the public API for creating, reading, updating, linking, and searching blocks.
- `MemoryScope`: a scoped CRUD/search handle intended to be passed into agents or sub-agents.
- `MemoryBackend`: the adapter contract for SQL, document, graph, vector, or composite backends.
- `RoutedBackend`: a composition backend for routing blocks across multiple stores.
- `MemoryHook`: lifecycle extension points around writes, reads, deletes, and search.
- `MemoryCodec`: value encoders/decoders for custom Python objects and storage-safe payloads.

Genie intentionally does not provide a full agent framework, durable production
storage in core, built-in embeddings, semantic search, planning, tool calling,
or a universal memory taxonomy.

## Quick Start

```python
from genie import InMemoryBackend, Memory

memory = Memory(backend=InMemoryBackend())

root = memory.put(type="task", value={"name": "write proposal"})
note = memory.put(
    parent=root,
    type="note",
    value="User prefers database-agnostic memory primitives.",
)

worker_memory = memory.scope(root=root, principal="agent:worker")
worker_memory.put(type="observation", value="The task has one design note.")

results = worker_memory.search("database-agnostic")
assert results[0].block.id == note.id
```

`Memory()` defaults to `InMemoryBackend()` when no backend is provided. The
examples pass `backend=InMemoryBackend()` explicitly so first-time readers can
see where storage comes from.

## Examples

Focused runnable examples live in [examples](examples):

- `crud_blocks.py`: CRUD, patching, delete, and child traversal.
- `scoped_agents.py`: top-level agents and sub-agents using `MemoryScope`.
- `search_filters.py`: text search, types, scopes, and metadata filters.
- `links.py`: typed links, symbolic mounts, and linked-memory search.
- `routed_backend.py`: route blocks across multiple backend stores.
- `policies.py`: principal and role based access control.
- `hooks_codecs.py`: lifecycle hooks with custom and stdlib value codecs.
- `stdlib_codecs.py`: opt-in stdlib codecs with decoded and raw payload examples.
- `basic_usage.py`: integrated workflow that combines the main features.

Run one directly from the repository root:

```bash
conda run -n "genie" python examples/basic_usage.py
```

## Core Model

Genie treats memory as a permissioned, typed, hierarchical graph of blocks.
Each block has identity, content, metadata, optional policy, and parent/child
placement. Blocks can also be connected with typed links.

```python
block = memory.put(
    type="preference",
    value={"style": "concise"},
    metadata={"source": "conversation"},
)

updated = memory.patch(block, value={"tone": "warm"})
```

Mounts are symbolic links between memory trees. The mount itself is a
`MemoryLink`; its source can be any memory block, and no special block `type` is
required. It points to another block without making that block an owned child,
so subtree search and recursive delete do not follow mounts by default.

```python
local_block = memory.put(type="project", value="local project")
remote_root = memory.put(type="project", value="external project")
mount = memory.add_mount(local_block, remote_root)
mounts_under_local = memory.discover_mounts(local_block)
mounted_root = memory.resolve_mount(mount)
```

Use `discover_mounts(root)` when an agent needs to inspect the mount links
available inside a subtree. Discovery returns mount links; resolving a selected
mount is still explicit.

## Query Filters

Use exact values for simple equality and operator dictionaries for comparisons:

```python
results = memory.search(
    "concise",
    types=["requirement"],
    filters={
        "metadata.priority": {">=": 3},
        "owner": {"in": ["team", "agent:planner"]},
    },
)
```

Genie accepts human-friendly operators in application code:

- `==` or `=` for equality
- `!=` for inequality
- `>`, `>=`, `<`, `<=` for comparisons
- `in` for membership

Internally these are normalized to canonical adapter-facing operators:

```python
{
    "metadata.priority": {"$gte": 3},
    "owner": {"$in": ["team", "agent:planner"]},
}
```

Adapters can rely on canonical names such as `$eq`, `$ne`, `$gt`, `$gte`,
`$lt`, `$lte`, and `$in`.

## Scoped Agent Memory

Sub-agents should receive a `MemoryScope`, not the whole memory object. A scope
supports normal CRUD/search operations while defaulting to subtree-only access,
with optional ancestor or global search.

```python
subtask_memory = memory.scope(root=block)
subtask_memory.search("style")          # subtree search

context_memory = memory.scope(root=block, allow_up=True)
context_memory.search("project", scope="ancestors")
```

Sub-scopes can narrow access, but cannot widen it. A sub-scope cannot add global
search, add ancestor traversal, mount access, change principal identity, or move
its root outside the parent scope's subtree.

## Backends

Users configure one top-level backend object:

```python
memory = Memory(backend=InMemoryBackend())
```

The included `InMemoryBackend` is useful for tests, examples, and local
prototypes. It is not durable storage. Database adapters should implement the
backend capabilities in `genie.backend`:

- `BlockStore`
- `TraversalIndex`
- `SearchIndex`
- `LinkStore`

That keeps stores, graph traversal, and search/indexing separable internally
while still allowing users to configure one top-level backend.

For sharding, data residency, migrations, or customer-owned storage,
`RoutedBackend` can coordinate multiple authoritative backend routes behind the
same public `MemoryBackend` interface. Each block still has exactly one
authoritative store route.

Routes can be added at runtime when an application discovers a mount or tenant
descriptor for a backend that was not opened at startup:

```python
backend.register_route("eu-prod", eu_backend)
mounted_root = memory.resolve_mount(mount)
```

## Operational Safety

Genie is designed so the core abstractions fail closed where possible:

- Scopes cannot widen delegated authority.
- Non-rootless scopes require an existing root block.
- Returned JSON values and metadata are copied so callers cannot mutate stored state accidentally.
- Recursive delete requires write access to every block in the deleted subtree.
- Recursive delete does not follow symbolic mounts.
- Recursive delete dispatches delete hooks for every removed block.
- In-memory hierarchy updates reject parent cycles.
- Link direction values are validated.
- Backend link stores expose cleanup for links connected to deleted blocks.
- Link listing hides relationships whose endpoints are not readable by the principal.
- Scoped link listing and linked-memory search do not expose blocks outside the scope.
- JSON encoding rejects non-finite floats such as `NaN` and `Infinity`.
- Unknown codecs and incompatible query filters raise clear errors or produce no matches.

The included `InMemoryBackend` is not durable storage. Use it for tests,
examples, and local prototypes; production deployments should provide a backend
adapter with the desired persistence, transactions, observability, and backup
behavior.

`principal` is the acting identity used by policies. Application code should
create scopes for the appropriate principal at trust boundaries and pass scopes,
not the unrestricted `Memory` object, into agents.

## Hooks

Hooks customize behavior around memory operations.

```python
from genie import InMemoryBackend, Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(backend=InMemoryBackend(), hooks=[AuditHook()])
```

## Codecs

Codecs translate user-level Python objects into storage-safe values and back.
`Memory()` registers only `JsonCodec` by default. Additional standard-library
codecs are opt-in:

```python
from decimal import Decimal

from genie import InMemoryBackend, Memory
from genie.codecs import CodecRegistry, JsonCodec
from genie.codecs.stdlib import DatetimeCodec, DecimalCodec, UUIDCodec

memory = Memory(
    backend=InMemoryBackend(),
    codecs=CodecRegistry([DatetimeCodec(), DecimalCodec(), UUIDCodec(), JsonCodec()]),
)

memory.put(type="amount", value=Decimal("0.1000000000000000001"))
```

The stdlib module includes `BytesCodec`, `DatetimeCodec`, `DecimalCodec`,
`UUIDCodec`, `DataclassCodec`, and `EnumCodec`. Register dataclass and enum
types explicitly so decoding only reconstructs trusted application types.

```python
from dataclasses import dataclass
from typing import Any

from genie import (
    CodecRegistry,
    EncodedValue,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryCodec,
)


@dataclass(frozen=True)
class Preference:
    topic: str
    value: str


class PreferenceCodec(MemoryCodec):
    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        return EncodedValue(self.content_type, {"topic": value.topic, "value": value.value})

    def decode(self, value: EncodedValue) -> Any:
        return Preference(**value.data)


memory = Memory(
    backend=InMemoryBackend(),
    codecs=CodecRegistry([JsonCodec(), PreferenceCodec()]),
)
memory.put(type="preference", value=Preference("style", "concise"))
```

## Development

Run Python through the project Conda environment:

```bash
conda run -n "genie" python -m pytest
```
