# Genie Design

Genie is a database-agnostic foundation for hierarchical agent memory. It is
not intended to be a full agent framework, vector database, graph database, or
opinionated long-term memory product. Its job is to define the core memory
model, scoped access semantics, and extension contracts that other systems can
build on.

The library is designed to:

- Provide a small, stable memory model that can be persisted in many backends.
- Treat memory as structured, hierarchical, and permissioned by default.
- Give agents scoped memory handles rather than unrestricted access to the full memory system.
- Keep storage, traversal, search, policy, hooks, and value encoding separable.
- Let adapter authors support SQL, document, graph, vector, or hybrid backends.
- Fail closed for access control and operational safety.
- Stay dependency-light in the core package.

Genie deliberately does not try to:

- prescribe a single database
- provide durable production storage in core
- implement embeddings or semantic search by default
- manage agent execution, planning, or tool calling
- define one universal memory taxonomy

## Mental Model

Genie models memory as a typed, hierarchical graph of blocks.

```text
Memory
  owns the backend, codecs, hooks, and policy engine

MemoryScope
  scoped CRUD/search handle passed to agents

MemoryBlock
  smallest independently addressable memory unit

MemoryLink
  typed relationship between two memory blocks

MemoryBackend
  adapter contract for persistence, traversal, links, and search
```

The key rule is:

```text
Memory is the full system. MemoryScope is a capability-bearing handle to part of it.
```

Application code should generally keep `Memory` at trust boundaries and pass
`MemoryScope` into agents or sub-agents.

## Package Layout

```text
genie/
  memory/
    block.py        # MemoryBlock identity, hierarchy, metadata, policy
    links.py        # MemoryLink relation model
    memory.py       # Memory public API
    scope.py        # MemoryScope scoped API
    errors.py       # Memory errors

  backend/
    capabilities.py # BlockStore, TraversalIndex, SearchIndex, LinkStore protocols
    base.py         # MemoryBackend protocol
    inmemory.py     # In-memory reference backend

  query/
    query.py        # MemoryQuery
    filters.py      # filter matching and operator normalization
    result.py       # SearchResult
    scope.py        # search scope enum

  policy/
    principal.py    # acting identity
    policy.py       # AccessRule and MemoryPolicy
    engine.py       # access decision engine

  codecs/
    base.py         # MemoryCodec and CodecRegistry
    json.py         # JSON-compatible default codec

  hooks/
    base.py         # MemoryHook extension methods
    registry.py     # hook dispatch
```

## Core Data Model

### MemoryBlock

`MemoryBlock` is the smallest independently addressable unit of memory.

```python
MemoryBlock(
    id="mem_...",
    type="note",
    value="User prefers concise answers.",
    parent_id="mem_parent",
    content_type="application/json",
    metadata={"source": "conversation"},
    policy=None,
    created_by="agent:planner",
    version=1,
)
```

A block owns:

- identity
- type
- value
- parent placement
- metadata
- policy
- timestamps
- creator
- version

Use a separate block when a piece of memory needs independent identity,
provenance, access control, searchability, lifecycle, or relationships.

### Values

The core library treats `value` as content-agnostic. The default `JsonCodec`
supports JSON-compatible values:

- `None`
- `str`
- `bool`
- `int`
- finite `float`
- `list`
- `dict` with string keys

Custom types can be supported through codecs.

```python
memory.put(type="preference", value={"style": "concise"})
memory.put(type="note", value="Keep the proposal short.")
```

### Hierarchy

Blocks form a parent/child hierarchy through `parent_id`.

```text
project
  task
    note
```

The hierarchy is used for scoped access, subtree search, ancestor traversal,
and recursive operations. The in-memory backend rejects parent cycles.

### Links

`MemoryLink` represents typed non-hierarchical relationships.

```python
memory.link(requirement, outline, "informs")
```

Links are useful when a relationship should not imply ownership or subtree
membership.

## Public API

### Memory

`Memory` is the full system handle.

```python
memory = Memory()
```

It exposes:

- `put`
- `get`
- `set`
- `patch`
- `delete`
- `children`
- `ancestors`
- `link`
- `links`
- `search`
- `scope`

`Memory` owns shared infrastructure:

- backend
- principal
- policy engine
- hook registry
- codec registry

### MemoryScope

`MemoryScope` is the agent-facing scoped handle.

```python
project_scope = memory.scope(root=project, principal="agent:planner")
task_scope = project_scope.scope(root=task)
```

It supports normal CRUD/search operations:

- `put`
- `get`
- `set`
- `patch`
- `delete`
- `children`
- `ancestors`
- `link`
- `links`
- `search`
- `scope`

The difference is that every operation is constrained to the scope root unless
explicitly configured otherwise.

## Scoped Access

Scopes are the main safety boundary in Genie.

```python
project_scope = memory.scope(root=project)
task_scope = project_scope.scope(root=task)
```

A sub-scope can narrow access but cannot widen it.

Specifically, a sub-scope cannot:

- change principal identity
- add ancestor traversal if the parent did not allow it
- add global search if the parent did not allow it
- move its root outside the parent scope's subtree

This makes delegated memory access monotonic:

```text
top-level agent gets project scope
sub-agent gets task scope
sub-sub-agent gets step scope
```

Each layer can pass down equal-or-smaller authority.

## Search Model

Search is represented by `MemoryQuery`.

```python
MemoryQuery(
    text="concise",
    root_id=project.id,
    scope=Scope.SUBTREE,
    types=("requirement",),
    filters={"metadata.priority": {"$gte": 3}},
    limit=20,
)
```

Supported scopes:

- `self`
- `subtree`
- `ancestors`
- `links`
- `global`

`Memory.search(...)` accepts a friendly call shape:

```python
memory.search(
    "concise",
    root=project,
    scope="subtree",
    types=["requirement"],
    filters={"metadata.priority": {">=": 3}},
)
```

## Filter Operators

Application code can use human-friendly operators:

```python
filters={
    "metadata.priority": {">=": 3},
    "owner": {"in": ["team", "agent:planner"]},
}
```

Genie normalizes these to canonical adapter-facing operators:

```python
filters={
    "metadata.priority": {"$gte": 3},
    "owner": {"$in": ["team", "agent:planner"]},
}
```

Supported aliases:

| Friendly | Canonical |
| --- | --- |
| `==` | `$eq` |
| `=` | `$eq` |
| `!=` | `$ne` |
| `>` | `$gt` |
| `>=` | `$gte` |
| `<` | `$lt` |
| `<=` | `$lte` |
| `in` | `$in` |

Adapters can rely on the canonical operator names. The in-memory backend
implements a small portable subset and fails closed for incompatible
comparisons.

## Backend Architecture

`MemoryBackend` is the top-level adapter contract. Users configure one backend:

```python
memory = Memory(backend=PostgresBackend(...))
```

Internally, that backend is described as a set of capabilities:

```text
BlockStore
  put_block
  get_block
  update_block
  delete_block

TraversalIndex
  children
  ancestors
  descendants
  contains

SearchIndex
  search

LinkStore
  add_link
  links
```

The important design choice is that storage and indexing are conceptually
separate even when one concrete backend implements both.

### Store vs Index

A store is the source of truth.

It answers questions like:

```text
What block has this id?
What is the current value and metadata?
What is this block's parent?
What should be persisted, updated, or deleted?
```

An index is a retrieval or traversal projection over that truth.

It answers questions like:

```text
Which blocks match this text query?
Which blocks are semantically similar?
Which descendants are under this root?
Which linked blocks should be returned?
Which blocks match this metadata filter efficiently?
```

Those responsibilities are related, but they are not the same.

For example:

```text
Postgres
  can be both the block store and the structured/text-search index.

Postgres + pgvector
  can store blocks and also provide vector retrieval.

S3 or object storage
  may be a store for large values, but not a good search index.

Elasticsearch
  may be an index for text retrieval, but not the source of truth.

Neo4j
  may be excellent for graph traversal and links, but a different system may own blobs.

Vector databases
  may retrieve similar blocks, but usually should not be the only source of truth for full memory state.
```

Keeping stores and indexes separate in the design is good because it preserves
clear ownership:

- writes go to the source of truth
- indexes can be rebuilt from stored blocks
- search failures do not necessarily imply data loss
- adapter authors can use the best database feature for each capability
- simple deployments can still use one database for everything
- advanced deployments can compose multiple systems without changing the public API

This is also an operational safety choice. If an index is stale, corrupted, or
temporarily unavailable, the system should still know where authoritative memory
lives. The store owns correctness; indexes optimize retrieval.

### One Public Backend, Multiple Internal Capabilities

The core API does not require users to choose separate store/index objects:

```python
memory = Memory(backend=PostgresBackend(...))
```

That keeps the user-facing setup simple. A developer using Genie should not
need to understand the internal storage topology just to pass memory into an
agent.

But inside the backend, the implementation can still be split:

```python
class CompositeBackend:
    store = SQLiteBlockStore(...)
    traversal = SQLiteTraversalIndex(...)
    search = ElasticsearchIndex(...)
    links = SQLiteLinkStore(...)
```

or implemented as one class:

```python
class PostgresBackend:
    def put_block(...): ...
    def get_block(...): ...
    def children(...): ...
    def search(...): ...
    def add_link(...): ...
```

Both shapes satisfy the same `MemoryBackend` contract.

The design therefore gives two layers:

```text
User-facing layer
  configure one backend

Adapter implementation layer
  implement store, traversal, search, and link capabilities however the database stack requires
```

This lets Genie stay database-agnostic without making the public API feel like
plumbing.

### Example Backend Shapes

A backend can implement all capabilities in one database or by composing
multiple systems.

Examples:

```text
Postgres backend
  blocks, hierarchy, metadata filters, full-text search

Postgres + pgvector backend
  blocks, hierarchy, filters, vector index

Neo4j backend
  graph relationships and traversal

S3 + vector database backend
  blob/object storage plus retrieval index
```

## InMemoryBackend

`InMemoryBackend` is the reference backend for tests and prototypes.

It provides:

- block storage
- parent/child traversal
- descendant traversal
- link storage
- simple text search
- simple metadata filters

It is not durable and should not be treated as production storage.

Operational safety currently includes:

- defensive copies for stored blocks and links
- parent-cycle rejection
- link direction validation
- recursive delete cleanup

## Policy Model

`Principal` is the acting identity.

```python
Principal(id="agent:writer")
Principal(id="user:alice", roles=frozenset({"admin"}))
```

`MemoryPolicy` controls operations:

- read
- write
- search
- traverse up
- traverse down

```python
MemoryPolicy(
    read=AccessRule.only(roles={"researcher"}),
    write=AccessRule.only(principals={"agent:planner"}),
)
```

The `PolicyEngine` checks whether a principal can perform an operation on a
block.

Important defaults:

- If no policy is attached to a block, the block is public.
- Scope boundaries are enforced in addition to block policies.
- Recursive delete requires write access to every block that would be deleted.

Root write access does not automatically imply write access to descendants.
That inheritance model can be added later, but it should be explicit.

## Hooks

Hooks let users customize behavior around operations without changing the core
memory model.

Available hook points:

- `before_put`
- `after_put`
- `before_get`
- `after_get`
- `before_search`
- `after_search`
- `before_delete`
- `after_delete`

Example:

```python
class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(hooks=[AuditHook()])
```

Common hook use cases:

- audit logging
- observability events
- automatic summarization
- external index updates
- redaction
- result reranking

Hooks should be treated as extension points, not as a replacement for backend
transactions.

## Codecs

Codecs translate user-level values into storage-safe values.

```python
class PreferenceCodec:
    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value):
        return isinstance(value, Preference)

    def encode(self, value):
        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value):
        return Preference(**value.data)
```

`CodecRegistry` chooses a codec by explicit `content_type` or by asking each
codec whether it can encode the value.

The default `JsonCodec` defensively copies encoded and decoded values so callers
cannot mutate stored state through returned objects.

## Operational Safety

The current implementation favors conservative behavior:

- scopes cannot widen authority
- unknown memory blocks raise clear errors for write-like operations
- recursive delete checks every affected block
- returned JSON values and metadata are copied
- in-memory reparenting rejects cycles
- link directions are validated
- JSON encoding rejects `NaN` and `Infinity`
- incompatible filter comparisons produce no matches

These are intentionally simple rules that adapter authors can preserve in more
advanced storage systems.

## Future TODOs

- backend contract test suite for adapter authors
- transaction abstraction
- batch operations
- async API variants
- policy inheritance modes
- explicit cascade-delete policies
- richer filter grammar
- pagination/cursors
- event stream or subscription hooks
- optional vector-search integration
- first-party SQLite/Postgres adapters

The current design keeps these additions possible without making the core
memory model depend on one database or retrieval strategy.
