# Backends

The `genie.backend` package separates Genie's public memory API from storage
and retrieval technology. A backend may use SQL, documents, graphs, vectors,
object storage, or several systems together.

Applications configure one top-level backend:

```python
memory = Memory(backend=MyBackend(...))
```

## Required interfaces

`MemoryBackend` combines four small protocols:

| Protocol | Responsibility |
| --- | --- |
| `BlockStore` | Create, read, update, and delete blocks |
| `TraversalIndex` | Parent/child traversal and containment |
| `SearchIndex` | Execute normalized `MemoryQuery` objects |
| `LinkStore` | Persist and list typed links |

The protocols use structural typing. An adapter does not need to inherit from a
Genie base class; it must implement the required methods and behavior.

One adapter class may implement every method. A more complex adapter may
delegate internally:

```python
class ApplicationBackend:
    blocks = PostgresBlockStore(...)
    traversal = PostgresTraversalIndex(...)
    links = Neo4jLinkStore(...)
    search_index = VectorSearchIndex(...)
```

That internal layout is an adapter decision. `Memory` still receives one
backend object.

## Store and index responsibilities

A store is the source of truth. It owns persisted blocks, links, and structural
state. An index optimizes search or traversal and should be rebuildable from
the source of truth.

Keeping those roles clear helps with recovery:

- writes commit to the source of truth;
- derived indexes may be rebuilt;
- index failure does not imply data loss; and
- adapters can choose the best database for each operation.

A single database may serve both roles. The distinction describes ownership,
not a required deployment topology.

## Reference backend

`InMemoryBackend` is the dependency-free reference implementation. It stores
blocks, links, keys, logs, and temporal entries in Python dictionaries and uses
a reentrant lock for structural atomicity.

Use it for tests, examples, and local prototypes. It is not a persistent
database. Production adapters should provide the required durability,
transactions, observability, backup, and concurrency behavior.

## Optional structure capabilities

Higher-level facades request only the extra storage they need:

| Protocol | Required behavior |
| --- | --- |
| `AcyclicLinkStore` | Atomically check for a cycle and insert an edge |
| `KeyedStore` | Unique key binding and compare-and-swap updates |
| `SequenceStore` | Atomic, monotonic sequence allocation |
| `TemporalStore` | Temporal membership and stable paging |
| `TemporalIndex` | Optional temporal query prefiltering |

These protocols are not part of `MemoryBackend`. A backend can support the
core API without supporting every optional structure. `VersionedRecords`
combines `KeyedStore` and `SequenceStore`; it adds no backend method.

For a SQLite adapter, for example, the adapter could use tables for blocks,
links, key bindings, log entries, and temporal entries. Unique constraints and
transactions would enforce the optional capability contracts. The facade code
would not need to change.

When optional storage is implemented by a separate internal component, the
top-level backend must still coordinate deletion cleanup and any required
transactions. Avoid splitting state in a way that leaves dangling structure
entries.

## Routed backends

`RoutedBackend` presents several `MemoryBackend` instances as one backend:

```python
backend = RoutedBackend(
    {"us": us_backend, "eu": eu_backend},
    default_route="us",
)
```

Each block has one owning route. By default, children remain on their parent's
route so subtree traversal and recursive deletion stay predictable.

The routed backend may use shared traversal, search, or link components.
Without a shared link store, cross-route links are rejected. Routes can be
registered later for application-managed mounts or tenant discovery.

`RoutedBackend` currently exposes the core `MemoryBackend` contract. Optional
keyed, sequence, temporal, and acyclic capabilities require a suitable store or
a capability-aware adapter around the routed deployment.

Mounts do not change route ownership. Discovering or resolving a mount also
does not automatically index or traverse the target route.

## Adapter requirements

A production adapter should preserve these behaviors:

- reject duplicate block and link ids;
- preserve parent/child integrity and prevent parent cycles;
- clean links and optional structural records when blocks are deleted;
- return stable ordering where a protocol requires paging;
- return defensive values that callers cannot mutate in place;
- keep compare-and-swap, sequence allocation, and DAG insertion atomic; and
- return normalized `SearchResult` objects for `MemoryQuery` requests.

Reusable mixins in
[`tests/structures/test_backend_contracts.py`](../../tests/structures/test_backend_contracts.py)
cover optional capability semantics and concurrent atomicity. Each new backend
should run the contracts for every capability it claims to support.

## Related guides

- [Memory](../memory/README.md)
- [Structures](../structures/README.md)
- [Queries](../query/README.md)
