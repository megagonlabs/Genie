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

Principal
  acting identity used by policy checks

MemoryPolicy
  block-level access rules for read, write, search, traversal, and custom
  operations

PolicyEngine
  evaluates whether a principal can perform an operation on a memory block
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
    structure_capabilities.py # optional DAG, keyed, sequence, and temporal protocols

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

  structures/
    __init__.py     # focused structure exports
    access.py       # shared Memory and MemoryHandle access protocols
    graph.py        # property-graph facade over typed links
    keyed.py        # namespaced keyed-collection facade
    lineage.py      # relation-scoped lineage DAG facade
    log.py          # ordered event-log facade
    records.py      # immutable structural membership records
    temporal.py     # temporal collection with scan and index query paths
    tree.py         # rooted ownership-tree facade
    versioned.py    # keyed identities with ordered revision history
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

### Structural Foundations

Optional memory structures use the shared protocols and records in
`genie.structures`. The protocols describe the common public operations of
`Memory` and `MemoryHandle`, allowing future structure facades to preserve
normal scope and policy checks. Facades should depend on the narrowest protocol
that provides the operations they need.

Structural records reference block ids rather than storing block values:

```python
from genie.structures import KeyBinding, MemoryAddress


address = MemoryAddress(namespace=("users", "alice"), key="language")
binding = KeyBinding(
    collection_id="preferences",
    address=address,
    block_id="mem_language",
)
```

This keeps application values in `MemoryBlock` and lets one block participate
in several structures. `ValidityInterval` uses aware UTC timestamps and the
half-open `[valid_at, invalid_at)` convention. `RevisionEntry.revision` is a
logical history position and is separate from `MemoryBlock.version`, which is
the update counter for one stored block.

The records do not provide persistence or query behavior. Structure facades
and optional backend capability protocols provide that behavior in separate
layers.

### Parent/Child Ownership

Blocks form parent/child ownership through `parent_id`.

```text
project
  task
    note
```

Parent/child ownership is used for scoped access, subtree search, ancestor traversal, and recursive operations. The in-memory backend rejects parent cycles.

`OwnershipTree` provides an optional rooted facade over this existing behavior:

```python
from genie.structures import OwnershipTree


tree = OwnershipTree(memory, root=project)
task = tree.create_child(type="task", value="Draft outline")
note = tree.create_child(parent=task, type="note", value="Keep it short.")

tree.children(task)
tree.ancestors(note)
tree.descendants()
```

The facade adds no new storage model. It delegates block creation, traversal,
policy checks, and recursive deletion to its `Memory` or `MemoryHandle` access
object. Its configured root is an additional boundary: operations cannot use a
parent or target outside that root, even when the facade receives an unbounded
`Memory`. Ancestor results stop at the root, and descendant results use
depth-first pre-order based on the child order returned by the access object.

### Links and Mounts

`MemoryLink` represents typed relationships that are separate from parent/child ownership.

```python
link = memory.add_link(requirement, outline, "informs")
link = memory.update_link(link, metadata={"confidence": 0.9})
```

Use links for relationships that do not imply ownership or subtree membership.

### Property Graph Facade

`PropertyGraph` uses `MemoryBlock` objects as nodes and existing `MemoryLink`
objects as directed typed edges. It delegates edge persistence and visibility
to `Memory` or `MemoryHandle` and adds access-aware neighbor and path traversal:

```python
from genie.structures import PropertyGraph


graph = PropertyGraph(memory)
edge = graph.add_edge(requirement, outline, "informs", metadata={"weight": 0.8})

graph.edges(requirement, direction="out")
graph.neighbors(requirement, relation="informs")
graph.walk(requirement, max_depth=2, limit=50)
graph.shortest_path(requirement, outline, max_depth=3)
```

The graph may contain cycles, self-links, and parallel edges. Neighbor results
contain both the visible block and the edge used to reach it, so parallel edges
do not lose their identity or metadata. `walk` returns simple paths in
breadth-first order; a block is not repeated within one path. Both `walk` and
`shortest_path` require a depth bound, and `walk` also has a result limit.

The facade does not define a separate graph root. Applications pass a bounded
`MemoryHandle` when graph reads and writes must remain inside a subtree. Normal
link endpoint policies and handle scope checks continue to apply at every step.
The facade does not add a graph database or a second link representation.

### Lineage DAG

`LineageDAG` is a relation-scoped acyclic view over the property graph. Its
edges are normal `MemoryLink` values. The source is a predecessor and the
target is a successor, so following outgoing edges walks toward derived
blocks.

```python
from genie.structures import LineageDAG


lineage = LineageDAG(memory, relation="derived_into")
lineage.add(raw_note, extracted_fact, metadata={"method": "extract"})
lineage.add(extracted_fact, summary)

lineage.predecessors(summary)
lineage.successors(raw_note)
lineage.walk_predecessors(summary, max_depth=4, limit=100)
```

The exact relation identifies the protected DAG. Applications use different
relation names when they need independent lineage graphs. The first protected
write may adopt existing links with that relation if they are already
acyclic. Once protected, ordinary link insertion for that relation must be
rejected or use the same cycle check.

A portable read-then-write check is not sufficient because two writers can
both observe an acyclic graph and then jointly create a cycle. The optional
`AcyclicLinkStore` therefore performs reachability checking and insertion as
one atomic operation. The reference backend holds its structure lock across
both steps. Production adapters should use a transaction, serializable graph
operation, advisory lock, or another database-specific atomic mechanism.

`Memory.add_acyclic_link` and `MemoryHandle.add_acyclic_link` perform normal
source-write, target-read, and handle-scope checks before calling that backend
operation. The facade reuses `PropertyGraph` for edge lookup, predecessor and
successor lookup, metadata updates, bounded walks, and shortest paths.

### Keyed Collection

`KeyedCollection` maps a `MemoryAddress(namespace, key)` to an existing visible
block. The binding is separate from ownership, so the addressed block may live
anywhere allowed by the supplied `Memory` or `MemoryHandle` and may participate
in other structures.

```python
from genie.structures import KeyedCollection, MemoryAddress


preferences = KeyedCollection(memory, backend, collection=preferences_root)
address = MemoryAddress(("users", "alice"), "language")
preferences.bind(address, language)

current = preferences.get(address)
preferences.rebind(address, french, expected=language)
preferences.unbind(address, expected=french)
```

`bind` is create-only. `rebind` and `unbind` include the expected block id and
the backend applies that check atomically. This compare-and-swap rule prevents
lost updates when concurrent writers use the same address. Namespace listing
may be exact or recursive and returns visible items in stable address order.
Exact lookup propagates access denial; listing omits inaccessible targets.

### Ordered Event Log

`EventLog` appends references to existing visible blocks. The backend allocates
one unique, monotonically increasing sequence number per log, starting at zero
in the reference backend.

```python
from genie.structures import EventLog


events = EventLog(memory, backend, log=events_root)
first = events.append(user_message)
second = events.append(agent_message)

events.read(start=first.entry.sequence, stop=second.entry.sequence + 1)
events.tail(limit=20)
```

Reads use half-open sequence ranges. A block may be appended more than once and
may appear in more than one log. Deleting a referenced block removes its log
entries but does not reuse their sequence numbers. Deleting the log block
removes the log entries and allocator state.

### Temporal Collection

`TemporalCollection` attaches a `TemporalEntry` to an existing visible block.
The entry keeps event time and validity separate: `occurred_at` records when an
event happened, while `ValidityInterval` records when information is valid.
One block has at most one entry in a collection, but the same block may appear
in several temporal collections.

```python
from datetime import UTC, datetime

from genie.structures import TemporalCollection, ValidityInterval


timeline = TemporalCollection(memory, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
    validity=ValidityInterval(
        valid_at=datetime(2026, 1, 1, tzinfo=UTC),
        invalid_at=datetime(2026, 2, 1, tzinfo=UTC),
    ),
)

events = timeline.events(
    start=datetime(2026, 1, 1, tzinfo=UTC),
    stop=datetime(2026, 2, 1, tzinfo=UTC),
)
current = timeline.valid_at(datetime(2026, 1, 15, tzinfo=UTC))
```

Event ranges and validity intervals use half-open bounds. `scan` can combine
event bounds, a valid-at instant, and an overlap interval. `replace` changes
the complete temporal entry, and `remove` removes only temporal membership; it
does not delete the referenced block.

The required `TemporalStore` returns bounded pages in stable block-id order.
When no `TemporalIndex` is available, the facade scans those pages and applies
time filters portably. A temporal index may prefilter the same query, but the
facade still checks the returned entries and resolves every block through the
supplied `Memory` or `MemoryHandle`. Exact lookup propagates access denial,
while scans omit inaccessible or missing targets.

Temporal results use block-id order so portable and indexed adapters have one
simple cursor contract. Use `EventLog` when insertion or chronological order is
part of the application model.

### Versioned Records

`VersionedRecords` composes `KeyedCollection` and `EventLog` without defining a
second persistence model. A `MemoryAddress` maps to one stable identity block.
That identity block anchors a separate ordered log of revision blocks. Log
sequence zero becomes logical revision one.

```python
from genie.structures import MemoryAddress, VersionedRecords


records = VersionedRecords(memory, backend, collection=records_root)
address = MemoryAddress(("users", "alice"), "favorite_color")

records.register(address, identity)
first = records.append(address, blue_revision)
second = records.append(address, green_revision)

records.current(address)
records.revision(address, 1)
records.history(address, start=1, stop=3)
```

`register` uses create-only key binding and the facade exposes no rebind
operation. `append` uses the sequence store's atomic per-log allocation, so
concurrent writers receive unique increasing revision numbers. Each identity
has an independent sequence. `RevisionEntry.record_id` is the identity block
id, and `RevisionEntry.revision` is the log sequence plus one.

Registration and first revision append are deliberately separate operations.
Combining them would imply a transaction across keyed and sequence storage
that not every backend can provide. `VersionedStore` is only the composition
of the existing `KeyedStore` and `SequenceStore` protocols; it adds no backend
method.

Applications should reserve registered identity blocks as revision-log anchors
and avoid appending unrelated events through a separate `EventLog`. They
should also treat revision blocks as immutable snapshots or deltas. Genie
preserves references and ordering but does not prevent trusted code from
updating a block through the normal memory API.

Deleting a revision block removes that log position without reusing its
revision number. Deleting an identity removes its key binding and complete log.
Exact revision lookup propagates access denial, while history and `current`
return the newest revisions visible through the supplied `Memory` or
`MemoryHandle`.

### Optional Structure Capabilities

`AcyclicLinkStore`, `KeyedStore`, `SequenceStore`, `TemporalStore`, and
`TemporalIndex` are optional protocols in
`genie.backend.structure_capabilities`; they are intentionally not added to the
required `MemoryBackend` interface. A backend implements only the structures it
supports. Facade construction raises `UnsupportedOperation` when the supplied
store lacks the required capability.

The reference `InMemoryBackend` implements these protocols with a reentrant lock.
The lock makes each key bind/rebind/unbind and each sequence allocation atomic
inside one process. Production adapters should use their storage engine's
unique constraints, compare-and-swap operation, transaction, or native sequence
support to provide the same contracts.

`TemporalIndex` is an optional query optimization, not a separate source of
truth. The reference backend implements its query contract with in-memory
filtering. Production adapters can use range or interval indexes. When a
separate index object is supplied, the adapter or application is responsible
for keeping it synchronized with the `TemporalStore`.

The facades attach existing blocks rather than hiding block creation inside the
structural write. This keeps payload creation, hooks, codecs, policies, and
structural membership explicit and avoids claiming a cross-store transaction
that a backend cannot provide.

### Composition and Backend Contracts

All seven facades can refer to the same `MemoryBlock` at once. Structural
records store block ids, so composition does not copy the block value or create
different identities for each view. Deleting a block asks the backend to clean
dependent links, key bindings, log entries, and temporal entries.

`LineageDAG` delegates graph reads, traversal, and metadata updates to
`PropertyGraph`; only insertion adds atomic cycle protection. Likewise,
`VersionedRecords` delegates stable identity lookup to `KeyedCollection` and
revision ordering to `EventLog`. Data written through `VersionedRecords` is
visible through those base facades, and compatible log entries written through
`EventLog` are visible as revisions.

Facades resolve their containers and referenced blocks through the supplied
`Memory` or `MemoryHandle`. A handle therefore applies its subtree boundary and
block policies across a compound memory view. Structural method availability
is capability-based: code that receives a configured facade or agent tool can
call the methods exposed by that object. Raw stores and backend capability
objects belong in trusted application code.

The reusable mixins in `tests/structures/test_backend_contracts.py` define the
optional backend contracts independently of facade behavior. A new repository
adapter can inherit the mixins for the capabilities it implements and provide
a fresh backend from `make_backend()`. The contracts cover uniqueness,
compare-and-swap behavior, stable paging, monotonic append, temporal filtering,
deletion cleanup, cycle prevention, and concurrent atomicity.

Link metadata can be merged after creation. The link id, source, target,
relation, and creation timestamp remain unchanged.

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
- `add_acyclic_link`
- `update_link`
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

`Memory.get_handle(root=...)` requires a concrete root, and the requested
handle principal must be able to read that root. Unbounded handles are created
with `Memory.get_unbounded_handle(...)` so administrative, routing, migration,
or other trusted workflows are explicit at the call site.

Mount operations on a `MemoryHandle` are available only when the handle is created with `allow_mounts=True`. Resolving a mount remains explicit and does not widen ordinary subtree search,
traversal, or delete behavior.

Handles cannot pass explicit block policies to `put(...)` unless they are created with `allow_policy_override=True`. Without that option, blocks created through a handle use the memory's
configured `default_policy`.

Handle default scopes are checked when the handle is created. For example, `default_scope="global"` requires `allow_global=True`, and `default_scope="ancestors"` requires `allow_up=True`.

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
- add policy override access if the parent did not allow it
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
  update_link
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

This keeps the backend storage layout inside adapter configuration. A developer using Genie does not need to understand the internal storage layout just to pass memory into an agent.

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
    def update_link(...): ...
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
  used by add_link, update_link, list_links, mounts, and linked-memory search
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

    def update_link(self, link):
        return self.graph.update_link(link)

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
- Handles cannot pass explicit policies to `put(...)` unless created with `allow_policy_override=True`.
- Recursive delete requires write access to every block that would be deleted.
- Unknown custom policy operations raise a configuration error.

Root write access does not automatically imply write access to descendants. Policy inheritance is not implemented in the current library.

## Security Model

Genie provides access-control building blocks, not a Python sandbox. Trusted application code should keep the full `Memory` object, backend adapters, hooks, codecs, and raw indexes. Agents and
sub-agents should usually receive `MemoryHandle` objects through application-controlled tool interfaces.

Applications should use explicit principals for users, agents, services, and supervisors. The default principal is useful for simple scripts, but shared systems should pass a concrete principal
when creating `Memory` and when creating handles.

New blocks are owner-private by default. Handles also reject explicit block policies by default, so untrusted agent code cannot make its own writes public or grant access to other principals.
Trusted code can create a handle with `allow_policy_override=True` when that handle is allowed to choose block-level policies.

Managed systems often need creator access plus supervisor access. `MemoryPolicy.owner_or(...)` supports that pattern without making blocks public:

```python
memory = Memory(
    principal="service:memory",
    default_policy=MemoryPolicy.owner_or(roles={"memory_admin"}),
)
```

Denied reads raise `AccessDenied`, while missing blocks return `None` through the full `Memory` API. That distinction helps developers find policy mistakes. If an application must hide whether
a block exists, wrap Genie at the application boundary and return a generic not-found response for both missing and denied reads.

Caller-provided block ids should be treated as trusted input. Duplicate-id errors can reveal that an id already exists. Applications that accept untrusted user input should prefer generated ids
or map external ids to internal Genie ids.

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
- handle default scopes cannot require disabled access
- handles cannot set explicit block policies unless created with `allow_policy_override=True`
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
