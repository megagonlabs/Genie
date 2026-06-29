# Genie

Genie is a database-agnostic foundation for hierarchical agent memory.

For a deeper architecture overview, see [docs/design.md](docs/design.md).

The core package defines the memory structure and extension contracts:

- `MemoryBlock`: the smallest independently addressable unit of memory.
- `Memory`: the public API for creating, reading, updating, linking, and searching blocks.
- `MemoryScope`: a scoped CRUD/search handle intended to be passed into agents or sub-agents.
- `MemoryBackend`: the adapter contract for SQL, document, graph, vector, or composite backends.
- `MemoryHook`: lifecycle extension points around writes, reads, deletes, and search.
- `MemoryCodec`: value encoders/decoders for custom Python objects and storage-safe payloads.

## Quick Start

```python
from genie import Memory

memory = Memory()

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
search, add ancestor traversal, change principal identity, or move its root
outside the parent scope's subtree.

## Backends

The included `InMemoryBackend` is useful for tests and prototypes. Database
adapters should implement the backend capabilities in `genie.backend`:

- `BlockStore`
- `TraversalIndex`
- `SearchIndex`
- `LinkStore`

That keeps stores, graph traversal, and search/indexing separable internally
while still allowing users to configure one top-level backend.

## Operational Safety

Genie is designed so the core abstractions fail closed where possible:

- Returned JSON values and metadata are copied so callers cannot mutate stored state accidentally.
- Recursive delete requires write access to every block in the deleted subtree.
- In-memory hierarchy updates reject parent cycles.
- Link direction values are validated.
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
from genie import MemoryHook


class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(hooks=[AuditHook()])
```

## Codecs

Codecs translate user-level Python objects into storage-safe values and back.

```python
from dataclasses import dataclass
from typing import Any

from genie import CodecRegistry, EncodedValue, JsonCodec, Memory, MemoryCodec


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


memory = Memory(codecs=CodecRegistry([JsonCodec(), PreferenceCodec()]))
memory.put(type="preference", value=Preference("style", "concise"))
```

## Development

Run Python through the project Conda environment:

```bash
conda run -n "genie" python -m pytest
```
