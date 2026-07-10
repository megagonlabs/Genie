# Genie Design

Genie is a utility library for developers building their own memory systems. It provides memory blocks, parent/child ownership, and scoped access without tying the application to one agent
platform, database, or memory use case.

Genie is not intended to be a full agent framework or an opinionated long-term memory product. It defines memory blocks, parent/child ownership rules, scoped access rules, and interfaces
developers can implement or plug into for storage, search, access control, observability, and value encoding.

Genie keeps application behavior outside the core package. Product-specific business logic, memory structure, agent workflows, retrieval strategies, storage layout, and value types remain
application-owned. Genie supplies memory building blocks and interfaces those systems can use.

## Design Objectives

- Stay platform-agnostic so applications can use Genie from LangChain, LlamaIndex, custom agent runtimes, services, scripts, or other tools.
- Stay storage/database-agnostic so adapters can target Redis, Postgres, document stores, graph databases, vector databases, object stores, or hybrid systems.
- Stay use-case-agnostic so applications can model conversation memory, NL2SQL memory, task/workflow memory, user preferences, domain knowledge, or other application-specific memory.
- Treat parent/child ownership as the default structure for scoped access and recursive operations.
- Keep business logic and agent behavior application-owned.
- Give agents scoped memory access rather than unrestricted access to the full memory system.
- Keep storage, traversal, search, policy, hooks, and value encoding separable.
- Provide hooks so applications can log, audit, measure, or customize memory operations.
- Allow developers to add adapters for SQL, document, graph, vector, or hybrid storage systems.
- Deny operations when access cannot be clearly verified.
- Keep the core package small and avoid unnecessary dependencies.

## Out of Scope

- Require or favor one specific database.
- Include a built-in production storage engine in the core package.
- Implement embeddings or semantic search by default.
- Manage agent execution, planning, or tool calling.
- Own application business logic or system design.
- Define one universal memory structure.

## Terminology

- `Memory`: The main interface for a Genie memory system. It configures the backend, codecs, hooks, policy engine, default principal, and default policy.
- `MemoryHandle`: A restricted handle to part of a `Memory` object. Agents and sub-agents should usually receive a handle instead of the full `Memory` object.
- `Codec`: A converter between application values and storage-safe data. For example, a codec can turn a dataclass into JSON-compatible data before storage and rebuild the dataclass when
  reading.
- `Principal`: The acting identity for an operation, such as an agent, user, or service. Policies use principals to decide access.
- `Policy`: Access rules attached to a block. Policies can control read, write, search, traversal, and application-defined custom operations.
- `Mount`: A symbolic link from one block to another memory tree. Mounts can point across backend routes without making the target tree part of the source subtree.
- `Hook`: User-provided code that runs before or after memory operations, such as create, read, search, or delete. Hooks are useful for audit logs, metrics, tracing, redaction, or external
  indexing.
- `RoutedBackend`: A backend that routes different blocks to different backend adapters while still presenting one `MemoryBackend` to the public API.

## Mental Model

Genie models memory as a typed graph of blocks. Parent/child relationships show ownership for scoped access. Links and mounts connect blocks without making one own the other. The `type` field
and the meaning of `value` are application-defined. Codecs convert values between application objects and storage-safe data.

```text
Memory
  main interface for a memory system; configures backend, codecs, hooks,
  policy engine, default principal, and default policy

MemoryHandle
  restricted interface for CRUD, traversal, links, mounts, and search

MemoryBlock
  smallest independently addressable memory unit

MemoryLink
  typed relationship between two memory blocks

MemoryBackend
  adapter interface for persistence, traversal, links, and search
```

Recommended integration pattern: keep `Memory` in trusted application code and pass `MemoryHandle` into agents or sub-agents.

## Package Layout

```text
genie/
  __init__.py       # top-level public exports
  py.typed          # typed package marker

  memory/
    __init__.py     # focused memory exports
    block.py        # MemoryBlock identity, parent placement, metadata, policy
    links.py        # MemoryLink relation model
    memory.py       # Memory public API
    handle.py       # MemoryHandle restricted API
    errors.py       # Memory errors

  backend/
    __init__.py     # backend exports
    capabilities.py # BlockStore, TraversalIndex, SearchIndex, LinkStore protocols
    base.py         # MemoryBackend interface
    inmemory.py     # in-memory reference backend
    routed.py       # routed multi-backend composition

  query/
    __init__.py     # query exports
    query.py        # MemoryQuery
    filters.py      # filter matching and operator normalization
    result.py       # SearchResult
    scope.py        # search scope enum

  policy/
    __init__.py     # policy exports
    principal.py    # acting identity
    policy.py       # AccessRule and MemoryPolicy
    engine.py       # access decision engine

  codecs/
    __init__.py     # codec exports
    base.py         # MemoryCodec and CodecRegistry
    json.py         # JSON-compatible default codec
    stdlib.py       # opt-in standard-library value codecs

  hooks/
    __init__.py     # hook exports
    base.py         # MemoryHook methods
    registry.py     # hook dispatch
```

## Core Data Model

### MemoryBlock

`MemoryBlock` is the smallest independently addressable unit of memory. Applications decide what each block means and how block types map to their own domain model.

```python
MemoryBlock(
    id="mem_...",
    type="note",
    value="User prefers concise answers.",
    parent_id="mem_parent",
    content_type="application/json",
    metadata={"source": "conversation"},
    policy=MemoryPolicy.owner_private(),
    created_by="agent:planner",
    version=1,
)
```

Use a separate block when a piece of memory needs its own id, source information, access control, search behavior, version history, or relationships.

### Values

The core library does not define what a block's `value` means. Applications choose the value shape, and codecs convert that value to and from storage-safe data. The default `JsonCodec` supports
JSON-compatible values:

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

### Parent/Child Ownership

Blocks form parent/child ownership through `parent_id`.

```text
project
  task
    note
```

Parent/child ownership is used for scoped access, subtree search, ancestor traversal, and recursive operations. The in-memory backend rejects parent cycles.

### Links and Mounts

`MemoryLink` represents typed relationships that are separate from parent/child ownership.

```python
memory.add_link(requirement, outline, "informs")
```

Use links for relationships that do not imply ownership or subtree membership.

Mounts are a reserved link pattern for symbolic cross-tree references. The mount itself is a `MemoryLink`; its source can be any memory block, and no special block `type` is required.

```python
mount = memory.add_mount(local_block, remote_root)
mounts = memory.discover_mounts(local_block)
remote_root = memory.resolve_mount(mount)
```

A mount points from a local source block to another memory root. Subtree traversal, subtree search, and recursive delete do not follow mounts by default. This matches filesystem symbolic link
behavior and keeps ownership separate from reference.

`discover_mounts(root)` scans a normal parent/child subtree and returns mount links whose source is inside that subtree. Discovery does not resolve mount targets and does not traverse into
mounted target trees.

## Public API

### Memory

`Memory` is the main developer interface. It exposes:

- `put`
- `get`
- `set`
- `update`
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
- `get_handle`
- `get_unbounded_handle`
- `contains`

`Memory` owns shared configuration:

- backend
- principal
- policy engine
- default policy
- hook registry
- codec registry

### MemoryHandle

`MemoryHandle` is the agent-facing restricted interface.

```python
project_handle = memory.get_handle(root=project, principal="agent:planner")
task_handle = project_handle.get_handle(root=task)
```

It supports normal CRUD, traversal, link, mount, search, and child handle operations. The difference is that every operation is constrained to the handle root unless explicitly configured
otherwise.

`MemoryHandle` does not expose `get_unbounded_handle()` or the raw `contains()` helper. Handle boundaries are enforced internally by scoped operations.

`Memory.get_handle(root=...)` requires a concrete root. Unbounded handles are created with `Memory.get_unbounded_handle(...)` so administrative, routing, migration, or other trusted workflows
are explicit at the call site.

Mount operations on a `MemoryHandle` are available only when the handle is created with `allow_mounts=True`. Resolving a mount remains explicit and does not widen ordinary subtree search,
traversal, or delete behavior.

## Scoped Access

`MemoryHandle` is the main way Genie limits what an agent or service can access.

```python
project_handle = memory.get_handle(root=project)
task_handle = project_handle.get_handle(root=task)
```

A child handle can narrow access but cannot increase it.

Specifically, a child handle cannot:

- change principal identity
- add ancestor traversal if the parent did not allow it
- add global search if the parent did not allow it
- add mount access if the parent did not allow it
- move its root outside the parent handle's subtree

This supports a common agent pattern:

```text
top-level agent gets project handle
sub-agent gets task handle
sub-sub-agent gets step handle
```

Each layer can pass down the same or less access.

## Search and Filters

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

`Memory.search(...)` provides a friendly call style:

```python
memory.search(
    "concise",
    root=project,
    scope="subtree",
    types=["requirement"],
    filters={"metadata.priority": {">=": 3}},
)
```

Application-facing filters accept human-friendly operators:

```python
filters={
    "metadata.priority": {">=": 3},
    "owner": {"in": ["team", "agent:planner"]},
}
```

Supported aliases:

| Friendly | Canonical |
| :-- | :-- |
| `==` | `$eq` |
| `=` | `$eq` |
| `!=` | `$ne` |
| `>` | `$gt` |
| `>=` | `$gte` |
| `<` | `$lt` |
| `<=` | `$lte` |
| `in` | `$in` |

Adapters receive canonical operator names.

## Backend Architecture

`MemoryBackend` is the top-level adapter interface. Users configure one backend:

```python
memory = Memory(backend=PostgresBackend(...))
```

Internally, that backend is described as a set of smaller interfaces:

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

The backend model separates storage and indexing conceptually, even when one concrete backend implements both.

### InMemoryBackend

`InMemoryBackend` is the included reference backend for tests, examples, and local prototypes.

Production deployments should provide a backend adapter with the desired persistence, transactions, observability, and backup behavior.

### Store vs Index

A store is the source of truth.

It answers questions like:

```text
What block has this id?
What is the current value and metadata?
What is this block's parent?
Which state is persisted, updated, or deleted?
```

An index is a search or traversal view built from stored block state.

It answers questions like:

```text
Which blocks match this text query?
Which blocks are similar by embedding or vector search?
Which descendants are under this root?
Which linked blocks are returned?
Which blocks match this metadata filter efficiently?
```

These responsibilities have different correctness, rebuild, and availability requirements.

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
  can retrieve similar blocks without acting as the source of truth for full
  memory state.
```

The separation defines ownership and recovery behavior:

- writes go to the source of truth
- indexes can be rebuilt from stored blocks
- search failures do not necessarily imply data loss
- adapter authors can use the appropriate database interface for each operation
- single-database deployments can still implement every interface in one place
- advanced deployments can compose multiple systems without changing the public API

Operationally, this separates saved memory state from derived search or traversal structures. If an index is stale, corrupted, or temporarily unavailable, the system still has a separate source
of truth for memory. The store owns correctness; indexes optimize lookup.

### One Public Backend, Multiple Internal Interfaces

The core API does not require users to choose separate store/index objects:

```python
memory = Memory(backend=PostgresBackend(...))
```

This keeps backend storage layout inside adapter configuration. A developer using Genie does not need to understand the internal storage layout just to pass memory into an agent.

Inside the backend, the implementation can still be split. `CompositeBackend`
is not a required Genie class or a fixed constructor shape. It is a common
adapter pattern: one public backend object coordinates whatever stores and
indexes the application needs.

```python
class CompositeBackend:
    blocks = PostgresBlockStore(...)
    values = ObjectValueStore(...)
    traversal = SQLiteTraversalIndex(...)
    links = SQLiteLinkStore(...)
    text = ElasticsearchIndex(...)
    vectors = VectorSearchIndex(...)
```

The field names and number of components are application choices. The example
names `traversal` and `links` explicitly because they map to separate backend
capabilities. A specialized composite can still back both capabilities with one
graph database, as shown below. The only requirement is that the outer object
implements the `MemoryBackend` methods that Genie calls.

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

Both shapes satisfy the same `MemoryBackend` interface.

Traversal and links may use the same graph database internally, but Genie keeps
them as separate capabilities:

```text
TraversalIndex
  parent/child ownership
  used by children, ancestors, descendants, contains, handles, and delete

LinkStore
  non-owning relationships
  used by add_link, list_links, mounts, and linked-memory search
```

Normal subtree traversal does not follow links. This keeps scoped handles,
recursive delete, and ownership checks predictable. A backend can still support
link-aware retrieval through `MemoryQuery(scope=Scope.LINKS)` or its own search
routing logic.

For example, a graph and vector backend can keep the full block record in a
canonical store while using graph and vector systems as derived indexes:

```python
class GraphVectorBackend:
    block_store = PostgresBlockStore(...)
    graph = Neo4jMemoryGraph(...)
    vector = VectorSearchIndex(...)

    def put_block(self, block):
        stored = self.block_store.put_block(block)
        self.graph.upsert_block(stored)
        self.graph.set_parent(stored.id, stored.parent_id)
        self.vector.upsert(stored)
        return stored

    def get_block(self, block_id):
        return self.block_store.get_block(block_id)

    def update_block(self, block):
        stored = self.block_store.update_block(block)
        self.graph.upsert_block(stored)
        self.graph.set_parent(stored.id, stored.parent_id)
        self.vector.upsert(stored)
        return stored

    def delete_block(self, block_id):
        self.block_store.delete_block(block_id)
        self.graph.delete_block(block_id)
        self.vector.delete(block_id)

    def children(self, block_id):
        ids = self.graph.child_ids(block_id)
        return self.block_store.get_blocks(ids)

    def ancestors(self, block_id):
        ids = self.graph.ancestor_ids(block_id)
        return self.block_store.get_blocks(ids)

    def descendants(self, block_id, *, include_self=False):
        ids = self.graph.descendant_ids(block_id, include_self=include_self)
        return self.block_store.get_blocks(ids)

    def contains(self, root_id, block_id):
        return self.graph.contains(root_id, block_id)

    def add_link(self, link):
        return self.graph.add_link(link)

    def list_links(self, block_id, **options):
        return self.graph.list_links(block_id, **options)

    def delete_links_for_block(self, block_id):
        self.graph.delete_links_for_block(block_id)

    def search(self, query):
        if query.scope == Scope.LINKS:
            return self.graph.search_linked_blocks(query)
        return self.vector.search(query)
```

In this shape, Postgres owns the saved `MemoryBlock` state. Neo4j owns
traversal and links. The vector index owns semantic retrieval. The public
`Memory` object still receives one backend object.

The design therefore gives two layers:

```text
User-facing layer
  configure one backend

Adapter implementation layer
  implement store, traversal, search, and link behavior however the database
  stack requires
```

As a result, Genie remains database-agnostic while backend composition stays out of the public API.

### Routed Backends

`RoutedBackend` is the built-in composition helper for using multiple stores behind one public `MemoryBackend`.

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

The public API still sees one backend. Internally, each block has exactly one route that owns it. The route is tracked by the backend catalog and resolved from stable routing rules.

```text
one block -> one owning store route
one Memory -> one public backend interface
```

`RoutedBackend` does not store database connections or storage handles on `MemoryBlock`. Blocks remain portable data objects. Routing stays in the backend layer.

Routes can also be registered after initialization:

```python
mounts = memory.discover_mounts(local_root)
route = mounts[0].metadata["route"]

if backend.route_for_block(mounts[0].target_id) is None:
    remote_backend = open_backend_from_mount_metadata(mounts[0].metadata)
    backend.register_route(route, remote_backend)

remote_root = memory.resolve_mount(mounts[0])
```

Runtime route registration supports mounts discovered during application execution. The mount stores route metadata. Application code uses that metadata to construct a backend adapter and
registers it with `register_route(route, backend)`. Existing `Memory` objects and `MemoryHandle` instances that share the routed backend can use the new route immediately.

By default, `RoutedBackend(enforce_tree_route=True)` keeps child blocks in the same route as their parent. This preserves subtree traversal, recursive delete, and scoped access behavior.
Independent root blocks can route to different stores. Advanced adapters may disable this setting, but then traversal, scoped access, and delete behavior must be handled by shared indexes or
equivalent backend logic.

Routed backends can use one or more indexes:

```text
Route-local indexes
  each route owns its own search/traversal/link interfaces
  global search fans out to routes and merges results

Shared indexes
  one search, traversal, or link index spans all routes
  useful for global retrieval or cross-route links

Hybrid indexes
  some interfaces are route-local and others are shared
  useful for tenant, region, or workload isolation
```

The routed model can therefore use multiple stores and multiple indexes. Stores own the saved block state; indexes remain derived search or traversal structures. A routed backend may have
several store routes, but each block has only one route that owns it.

Cross-route links require a shared `LinkStore`, such as `InMemoryLinkStore` for tests and prototypes. Without a shared link store, route-local link stores can only represent relationships whose
endpoints live in the same route.

Cross-route mounts use the same shared link interface with the reserved `LinkRelations.MOUNT` relation (`genie.mount`). They point from one routed tree into another routed tree without changing
ownership. The default `RoutedBackend` behavior keeps parent/child ownership inside the parent route.

`discover_mounts(root)` can find cross-route mount links whose source is inside the scanned tree when the routed backend has a shared link store. If a mount target route has not been registered
yet, mount discovery can still return the pending mount link; `resolve_mount(mount)` returns `None` until application code registers the target route. If a target block is known but not
readable, discovery hides that mount.

Following mounts during traversal/search and deleting mounted target trees are not implemented in the current library.

The public `Memory.add_mount()` and `MemoryHandle.add_mount()` methods validate both endpoints through the active backend. Pending mounts to not-yet-registered routes are therefore represented
by adapter, migration, or import code that can write a symbolic `MemoryLink` to a shared `LinkStore` before the target route is opened. After `register_route()` adds the route, the same mount
can resolve through the public memory API.

`register_route()` updates the set of backend routes. It does not backfill external shared search or traversal indexes. Applications that provide shared indexes are responsible for indexing
newly registered route contents according to their own operational model.

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
- application-defined custom operations

```python
MemoryPolicy(
    read=AccessRule.only(roles={"researcher"}, owner=True),
    write=AccessRule.owner(),
    search=AccessRule.only(roles={"researcher"}, owner=True),
    traverse_up=AccessRule.only(roles={"researcher"}, owner=True),
    traverse_down=AccessRule.only(roles={"researcher"}, owner=True),
    operations={"export": AccessRule.only(roles={"compliance"})},
)
```

Applications can configure the default policy attached to new blocks:

```python
memory = Memory(default_policy=MemoryPolicy.owner_private())
```

`MemoryPolicy.owner_private()` is the default when `default_policy` is not provided. Applications can choose another default, such as `MemoryPolicy.public()`, when a system needs broader
access.

Managed memory systems often need both creator access and supervisor access.
Use `owner_or(...)` for that pattern:

```python
memory = Memory(
    principal="service:memory",
    default_policy=MemoryPolicy.owner_or(roles={"memory_admin"}),
)
```

With this policy, blocks created through agent handles keep the agent as
`created_by`, while principals with the configured role or id can also access
those blocks.

The `PolicyEngine` checks whether a principal can perform an operation on a block.

Applications can register operation defaults on the engine:

```python
policy_engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
```

Custom operations are evaluated explicitly by application code with `policy_engine.require("export", block, principal)` or `policy_engine.allows("export", block, principal)`.

Important defaults:

- New blocks use `MemoryPolicy.owner_private()` unless `Memory(default_policy=...)` or `put(..., policy=...)` chooses another policy.
- Stored blocks with no policy are treated as owner-private by using `MemoryBlock.created_by`.
- Scope limits are enforced in addition to block policies.
- Recursive delete requires write access to every block that would be deleted.
- Unknown custom policy operations raise a configuration error.

Root write access does not automatically imply write access to descendants. Policy inheritance is not implemented in the current library.

## Hooks

Hooks let users customize behavior before and after operations without changing the core memory model. They are the main way to add application-level observability, such as audit logs, metrics,
tracing events, and operation logs around memory creates, reads, deletes, and searches.

Hooks run inside the public operation flow. Genie keeps structure and access checks around hook execution:

- `before_put` hooks cannot change parent placement after it has been checked.
- Search queries are checked again after `before_search` hooks run.
- Search results are checked again after `after_search` hooks run.

This lets hooks customize operations without letting them bypass scope or policy checks.

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

`CodecRegistry` chooses a codec by explicit `content_type` or by asking each codec whether it can encode the value.

The default `JsonCodec` copies encoded and decoded values so callers cannot mutate stored state through returned objects.

`Memory()` registers only `JsonCodec` by default. Additional dependency-free codecs live in `genie.codecs.stdlib` and are opt-in:

- `BytesCodec`
- `DatetimeCodec`
- `DecimalCodec`
- `UUIDCodec`
- `DataclassCodec`
- `EnumCodec`

Stdlib codecs encode values into portable payload shapes, usually JSON-compatible data.

## Operational Safety

The current implementation uses cautious behavior:

- child handles cannot increase access
- bounded handles require an existing root block
- unbounded handles must be created explicitly and require an explicit root for non-global search
- full `Memory.get` returns `None` for missing blocks; rooted handles treat missing or out-of-bounds ids as denied before fetching
- recursive delete requires write access to every affected block
- recursive delete does not follow symbolic mounts
- recursive delete dispatches delete hooks for every removed block
- `before_put` hooks cannot change parent placement after authorization
- search hooks are re-authorized and results are re-filtered by policy and scope
- returned JSON values and metadata are copied
- in-memory reparenting rejects cycles
- link directions are validated
- backend link stores expose cleanup for links connected to deleted blocks
- link listing hides relationships whose endpoints are not readable by the principal
- mount discovery does not follow mounted target trees
- scoped link listing and linked-memory search remain inside the handle root
- JSON encoding rejects `NaN` and `Infinity`
- incompatible filter comparisons produce no matches

These rules define the basic safety behavior for developers building adapters for more complex storage systems.

## FAQ / Common Implementation Questions

### How does policy inheritance work?

Root write access does not automatically grant write access to descendant blocks. Genie evaluates permissions per block, so access to a parent block does not imply access to its children.

### Where do I draw the line between Store and Index?

A store is the source of truth. Writes and updates should target the store so stored memory stays consistent. An index is derived from stored memory and is optimized for retrieval or traversal.
Adapter implementations should treat store methods as the source of truth and index methods as rebuildable from stored state.

### What happens if I link to something I cannot read?

`list_links` excludes relationships when the principal lacks read access to either endpoint. If linked search encounters an unreadable block, that block is omitted from the current principal's
results.

### How do I register a custom codec at runtime?

Custom codecs can be registered on the codec registry used by a `Memory` handle:

```python
memory.codecs.register(PreferenceCodec())
```

Existing handles created from that `Memory` share the same codec registry.
