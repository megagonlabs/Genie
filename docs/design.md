# Genie Design

Genie is a utility library for developers building their own memory systems.
It provides memory blocks, parent/child containment, scoped access, and
extension contracts without tying the application to one agent platform,
database, or memory use case. It is not intended to be a full agent framework,
vector database, graph database, or opinionated long-term memory product. It
defines a memory block model, containment semantics, scoped access semantics,
and extension contracts that other systems can build on.

Genie keeps application behavior outside the core package. Product-specific
business logic, memory taxonomies, agent workflows, retrieval strategies,
storage topology, and value types remain application-owned. Genie supplies the
structural primitives and adapter boundaries those systems can compose.

Design objectives:

- Stay platform-agnostic so applications can use Genie from LangChain,
  LlamaIndex, custom agent runtimes, services, scripts, or other orchestration
  layers.
- Stay storage/database-agnostic so adapters can target Redis, Postgres,
  document stores, graph databases, vector databases, object stores, or hybrid
  systems.
- Stay use-case-agnostic so applications can model conversation memory, NL2SQL
  memory, task/workflow memory, user preferences, domain knowledge, or other
  application-specific memory.
- Provide a small, stable memory model that can be persisted in many backends.
- Treat parent/child containment as the default ownership structure for scoped
  access and recursive operations.
- Keep business logic, memory taxonomy, and agent behavior application-owned.
- Give agents scoped memory handles rather than unrestricted access to the full memory system.
- Keep storage, traversal, search, policy, hooks, and value encoding separable.
- Provide observability extension points so applications can audit or log memory
  operations; public memory lifecycle events are exposed through hooks, while
  database-native operation logs remain the responsibility of backend adapters.
- Let adapter authors support SQL, document, graph, vector, or hybrid backends.
- Fail closed for access control and operational safety.
- Stay dependency-light in the core package.

Out of scope:

- Prescribe a single database.
- Provide durable production storage in core.
- Implement embeddings or semantic search by default.
- Manage agent execution, planning, or tool calling.
- Own application business logic or system design.
- Define one universal memory taxonomy.

## Mental Model

Genie models memory as a typed graph of blocks. Parent/child relationships
provide containment for scoped access; links and mounts provide non-containment
relationships. The `type` field is application-defined, and the `value` payload
is content-agnostic behind the codec interface.

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

Recommended integration pattern: keep `Memory` at trust boundaries and pass
`MemoryScope` into agents or sub-agents.

## Package Layout

```text
genie/
  memory/
    block.py        # MemoryBlock identity, parent placement, metadata, policy
    links.py        # MemoryLink relation model
    memory.py       # Memory public API
    scope.py        # MemoryScope scoped API
    errors.py       # Memory errors

  backend/
    capabilities.py # BlockStore, TraversalIndex, SearchIndex, LinkStore protocols
    base.py         # MemoryBackend protocol
    inmemory.py     # In-memory reference backend
    routed.py       # routed multi-backend composition

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
    stdlib.py       # opt-in standard-library value codecs

  hooks/
    base.py         # MemoryHook extension methods
    registry.py     # hook dispatch
```

## Core Data Model

### MemoryBlock

`MemoryBlock` is the smallest independently addressable unit of memory. It is
the primitive Genie uses to track parent/child containment; applications decide
what each block means and how block types map to their own domain model.

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

Custom type support is provided through codecs.

```python
memory.put(type="preference", value={"style": "concise"})
memory.put(type="note", value="Keep the proposal short.")
```

### Parent/Child Containment

Blocks form parent/child containment through `parent_id`.

```text
project
  task
    note
```

Parent/child containment is used for scoped access, subtree search, ancestor
traversal, and recursive operations. The in-memory backend rejects parent
cycles.

### Links

`MemoryLink` represents typed non-hierarchical relationships.

```python
memory.add_link(requirement, outline, "informs")
```

Use links for relationships that do not imply ownership or subtree membership.

Mounts are a reserved link pattern for symbolic cross-tree references. The
mount itself is a `MemoryLink`; its source can be any memory block, and no
special block `type` is required.

```python
mount = memory.add_mount(local_block, remote_root)
mounts = memory.discover_mounts(local_block)
remote_root = memory.resolve_mount(mount)
```

A mount points from a local source block to another memory root. The target does
not become a child of the source. Subtree traversal, subtree search, and
recursive delete do not follow mounts by default. Deleting the source block
deletes the mount link, not the mounted target tree. This matches filesystem
symbolic link behavior and keeps ownership separate from reference.

`discover_mounts(root)` scans a normal parent/child subtree and returns mount
links whose source is inside that subtree. Discovery does not resolve mount
targets and does not traverse into mounted target trees.

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
- `add_link`
- `list_links`
- `add_mount`
- `list_mounts`
- `discover_mounts`
- `resolve_mount`
- `search`
- `scope`
- `unbounded_scope`
- `contains`

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
- `add_link`
- `list_links`
- `add_mount`
- `list_mounts`
- `discover_mounts`
- `resolve_mount`
- `search`
- `scope`

The difference is that every operation is constrained to the scope root unless
explicitly configured otherwise.

`Memory.scope(root=...)` requires a concrete root. Unbounded delegated access is
created with `Memory.unbounded_scope(...)` so administrative, routing,
migration, or other trusted workflows are explicit at the call site.

Mount operations on a `MemoryScope` are available only when the scope is created
with `allow_mounts=True`. Resolving a mount remains explicit and does not widen
ordinary subtree search, traversal, or delete behavior.

## Scoped Access

Scopes define the primary safety boundary in Genie.

```python
project_scope = memory.scope(root=project)
task_scope = project_scope.scope(root=task)
```

A sub-scope can narrow access but cannot widen it.

Specifically, a sub-scope cannot:

- change principal identity
- add ancestor traversal if the parent did not allow it
- add global search if the parent did not allow it
- add mount access if the parent did not allow it
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

Application-facing filters accept human-friendly operators:

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

Adapters receive canonical operator names. The in-memory backend
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
  list_links
  delete_links_for_block
```

The backend model separates storage and indexing conceptually, even when one
concrete backend implements both.

### Store vs Index

A store is the source of truth.

It answers questions like:

```text
What block has this id?
What is the current value and metadata?
What is this block's parent?
Which state is persisted, updated, or deleted?
```

An index is a retrieval or traversal projection over that truth.

It answers questions like:

```text
Which blocks match this text query?
Which blocks are semantically similar?
Which descendants are under this root?
Which linked blocks are returned?
Which blocks match this metadata filter efficiently?
```

These responsibilities have different correctness, rebuild, and availability
requirements.

For example:

```text
Postgres
  can be both the block store and the structured/text-search index.

Postgres + pgvector
  can store blocks and also provide vector retrieval.

S3 or object storage
  can store large values while relying on another system for search.

Elasticsearch
  may be an index for text retrieval, but not the source of truth.

Neo4j
  can own graph traversal and links while a different system owns blobs.

Vector databases
  can retrieve similar blocks without acting as the source of truth for full memory state.
```

The separation defines ownership and recovery behavior:

- writes go to the source of truth
- indexes can be rebuilt from stored blocks
- search failures do not necessarily imply data loss
- adapter authors can use the appropriate database capability for each operation
- single-database deployments can still implement every capability in one place
- advanced deployments can compose multiple systems without changing the public API

Operationally, this separates authoritative state from derived retrieval
structures. If an index is stale, corrupted, or temporarily unavailable, the
system still has a separate authoritative memory source. The store owns
correctness; indexes optimize retrieval.

### One Public Backend, Multiple Internal Capabilities

The core API does not require users to choose separate store/index objects:

```python
memory = Memory(backend=PostgresBackend(...))
```

This keeps backend topology internal to adapter configuration. A developer using
Genie does not need to understand the internal storage topology just to pass
memory into an agent.

Inside the backend, the implementation can still be split:

```python
class CompositeBackend:
    store = SQLiteBlockStore(...)
    traversal = SQLiteTraversalIndex(...)
    search = ElasticsearchIndex(...)
    link_store = SQLiteLinkStore(...)
```

or implemented as one class:

```python
class PostgresBackend:
    def put_block(...): ...
    def get_block(...): ...
    def children(...): ...
    def search(...): ...
    def add_link(...): ...
    def list_links(...): ...
    def delete_links_for_block(...): ...
```

Both shapes satisfy the same `MemoryBackend` contract.

The design therefore gives two layers:

```text
User-facing layer
  configure one backend

Adapter implementation layer
  implement store, traversal, search, and link capabilities however the database stack requires
```

As a result, Genie remains database-agnostic while backend composition stays out
of the public API.

### Example Backend Shapes

A backend can implement all capabilities in one database or by composing
multiple systems.

Examples:

```text
Postgres backend
  blocks, parent placement, metadata filters, full-text search

Postgres + pgvector backend
  blocks, parent placement, filters, vector index

Neo4j backend
  graph relationships and traversal

S3 + vector database backend
  blob/object storage plus retrieval index
```

### Routed Backends

`RoutedBackend` is the built-in composition helper for using multiple
authoritative stores behind one public `MemoryBackend`.

```python
backend = RoutedBackend(
    {
        "us": USPostgresBackend(...),
        "eu": EUPostgresBackend(...),
    },
    route_selector=lambda block: block.metadata.get("region"),
    default_route="us",
)

memory = Memory(backend=backend)
```

The public API still sees one backend. Internally, each block has exactly one
authoritative route. The route is tracked by the backend catalog and resolved
from stable routing rules such as tenant, region, namespace, root subtree, block
type, or migration phase.

The source-of-truth rule remains:

```text
one block -> one authoritative store route
one Memory -> one public backend contract
```

`RoutedBackend` does not put database connections or storage handles on
`MemoryBlock`. Blocks remain portable data objects. Routing stays in the
backend layer.

Routes can also be registered after initialization:

```python
mounts = memory.discover_mounts(local_root)
route = mounts[0].metadata["route"]

if backend.route_for_block(mounts[0].target_id) is None:
    remote_backend = open_backend_from_mount_metadata(mounts[0].metadata)
    backend.register_route(route, remote_backend)

remote_root = memory.resolve_mount(mounts[0])
```

Runtime route registration supports mount or tenant descriptors discovered
during application execution. The mount stores route metadata, not a live
database connection. Application code uses that metadata to construct a backend
adapter and registers it with `register_route(route, backend)`. Existing
`Memory` and `MemoryScope` handles that share the routed backend can use the
new route immediately.

By default, `RoutedBackend(enforce_tree_route=True)` keeps child blocks in the
same route as their parent. This preserves subtree traversal, recursive delete,
and scoped access semantics. Independent root blocks can route to different
stores. Advanced adapters may disable this setting, but then traversal,
containment, scoped access, and delete semantics must be handled by shared
indexes or equivalent backend logic.

Routed backends can use one or more indexes:

```text
Route-local indexes
  each route owns its own search/traversal/link capabilities
  global search fans out to routes and merges results

Shared indexes
  one search, traversal, or link index spans all routes
  useful for global retrieval or cross-route links

Hybrid indexes
  some capabilities are route-local and others are shared
  useful for tenant, region, or workload isolation
```

The routed model can therefore use multiple stores and multiple indexes. Stores
own authoritative block state; indexes remain derived retrieval or traversal
structures. A routed backend may have several store authorities, but only one
authority for any given block.

Cross-route links require a shared `LinkStore`, such as `InMemoryLinkStore` for
tests and prototypes. Without a shared link store, route-local link stores can
only represent relationships whose endpoints live in the same route.

Cross-route mounts use the same shared link capability with the reserved
`LinkRelations.MOUNT` relation (`genie.mount`). They point from one routed tree
into another routed tree without changing ownership. The default
`RoutedBackend` behavior keeps parent/child ownership inside the parent route.
`discover_mounts(root)` can find cross-route mount links whose source is inside
the scanned tree when the routed backend has a shared link store. If a mount
target route has not been registered yet, mount discovery can still return the
pending mount link; `resolve_mount(mount)` returns `None` until application code
registers the target route. If a target block is known but not readable,
discovery hides that mount from the current principal. Following mounts during
traversal/search and deleting mounted target trees are not implemented in the
current library.

The public `Memory.add_mount()` and `MemoryScope.add_mount()` methods validate
both endpoints through the active backend. Pending mounts to not-yet-registered
routes are therefore represented by adapter, migration, or import code that can
write a symbolic `MemoryLink` to a shared `LinkStore` before the target route is
opened. After `register_route()` adds the route, the same mount can resolve
through the public memory API.

`register_route()` updates the set of authoritative stores. It does not
backfill external shared search or traversal indexes. Applications that provide
shared indexes are responsible for indexing newly registered route contents
according to their own operational model.

## InMemoryBackend

`InMemoryBackend` is the reference backend for tests and prototypes.

It provides:

- block storage
- parent/child traversal
- descendant traversal
- link storage
- basic text search
- basic metadata filters

It is not durable and is intended for tests, examples, and local prototypes.

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
- application-defined extension operations

```python
MemoryPolicy(
    read=AccessRule.only(roles={"researcher"}),
    write=AccessRule.only(principals={"agent:planner"}),
    operations={"export": AccessRule.only(roles={"compliance"})},
)
```

The `PolicyEngine` checks whether a principal can perform an operation on a
block.

Applications can register operation defaults on the engine:

```python
policy_engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
```

Custom operations are evaluated explicitly by application code with
`policy_engine.require("export", block, principal)` or
`policy_engine.allows("export", block, principal)`. Built-in memory methods use
the built-in operations directly. Custom operation names cannot override
`read`, `write`, `search`, `traverse_up`, or `traverse_down`.

Important defaults:

- If no policy is attached to a block, the block is public.
- Scope boundaries are enforced in addition to block policies.
- Recursive delete requires write access to every block that would be deleted.
- Unknown custom policy operations raise a configuration error.

Root write access does not automatically imply write access to descendants.
That inheritance model can be added later as an explicit policy mode.

## Hooks

Hooks let users customize behavior around operations without changing the core
memory model. They are the primary extension point for application-level
observability, such as audit logs, metrics, tracing events, and operation logs
around memory creates, reads, deletes, and searches. Backend adapters can add
database-native logging when an application needs every underlying Redis,
Postgres, or graph database operation recorded.

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

Hooks are extension points, not a replacement for backend transactions.

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

`Memory()` registers only `JsonCodec` by default. Additional dependency-free
codecs live in `genie.codecs.stdlib` and are opt-in:

- `BytesCodec`
- `DatetimeCodec`
- `DecimalCodec`
- `UUIDCodec`
- `DataclassCodec`
- `EnumCodec`

Stdlib codecs encode values into portable payload shapes, usually JSON-compatible
data. `DataclassCodec` and `EnumCodec` require trusted application types to be
registered with the codec; decoding does not import arbitrary Python classes by
name.

## Operational Safety

The current implementation favors conservative behavior:

- scopes cannot widen authority
- bounded scopes require an existing root block
- unbounded scopes must be created explicitly and require an explicit root for
  non-global search
- `get` returns `None` for missing blocks, while operations that require an
  existing block raise clear errors
- recursive delete checks every affected block
- recursive delete does not follow symbolic mounts
- recursive delete dispatches delete hooks for every removed block
- returned JSON values and metadata are copied
- in-memory reparenting rejects cycles
- link directions are validated
- backend link stores expose cleanup for links connected to deleted blocks
- link listing hides relationships whose endpoints are not readable by the principal
- mount discovery does not follow mounted target trees
- scoped link listing and linked-memory search remain inside the scope root
- JSON encoding rejects `NaN` and `Infinity`
- incompatible filter comparisons produce no matches

These rules define the baseline safety behavior for adapter authors targeting
more advanced storage systems.

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
- explicit follow-mount traversal/search modes
- first-party SQLite/Postgres adapters

These additions remain compatible with the current core abstractions without
making the memory model depend on one database or retrieval strategy.

## FAQ / Common Implementation Questions

### How does policy inheritance work?

Root write access does not automatically grant write access to descendant
blocks. Genie evaluates permissions per block, so access to a parent block does
not imply authorization for its children.

### Where do I draw the line between Store and Index?

A store is the source of truth. Writes and updates should target the store so
the authoritative memory state remains consistent. An index is a derived
projection optimized for retrieval or traversal. Adapter implementations should
treat store methods as authoritative and index methods as rebuildable from
stored state.

### What happens if I link to something I cannot read?

`list_links` excludes relationships when the principal lacks read access to
either endpoint. If linked search encounters an unauthorized block, that block
is omitted from the current principal's results.

### How do I register a custom codec at runtime?

Custom codecs can be registered on the codec registry used by a `Memory`
handle:

```python
memory.codecs.register(PreferenceCodec())
```

Existing scopes created from that `Memory` share the same codec registry.
