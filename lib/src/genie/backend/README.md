# Backends

`genie.backend` keeps the public memory API separate from storage and
retrieval. An application gives `Memory` one top-level backend. That backend
can use a single database, or it can coordinate several stores and indexes.

```python
memory = Memory(backend=MyBackend(...))
```

## Core contract

`MemoryBackend` has 4 protocols, and Python matches them by structure. An
adapter does not inherit from a Genie base class. It only gives the required
behavior.

| Protocol | Responsibility |
| --- | --- |
| `BlockStore` | Create, read, conditionally update, and delete blocks |
| `TraversalIndex` | Parent and child traversal, and containment |
| `SearchIndex` | Run normalized `MemoryQuery` values |
| `LinkStore` | Store and list typed links |

An adapter can implement each method itself, or give the work to SQL, graph,
vector, document, or other components inside it. In both cases, `Memory` gets
one backend object.

### Conditional writes

- **`update_block(..., expected_version=...)`** succeeds only when the stored version matches. The contract requires a replacement version that is one higher.
- **`update_block` keeps the block identity.** A replacement that changes the `type`, `created_at`, `created_by`, or `policy` of the stored block raises `MemoryConflict`, with or without `expected_version`. So a replacement made from a deleted block cannot change a new block with the same id and version.
- **`delete_block(..., expected_blocks=...)`** succeeds only when the given blocks match the current subtree exactly: the same ids, versions, and identity fields.
- **`Memory.set`, `Memory.update`, and `Memory.delete`** give these expectations after they authorize the request.
- **The contract requires atomicity.** A backend checks and changes state in one atomic step. So no other writer can come in between the check and the change.
- **`None` skips the version check.** So trusted backend-level administration can make an unconditional change. The identity check still runs.

### Retried commits

- **A lost reply looks the same as a lost commit.** An adapter that talks to a server over a network can lose the reply to a commit that the server already applied. So a retry can run the same operation a second time.
- **An adapter that retries makes each operation safe to repeat.** Either its own uniqueness check rejects the repeat, or the repeat ends at the same state.
- **`append_log_entry` is the exception.** It has no uniqueness key, so a repeat appends the same block again at a new sequence.

### Errors

- **`MemoryConflict`** is for expected conflicts of identity, version, or structure.
- **A `BackendError` subclass** is for storage failures. The adapter keeps the original driver error as `__cause__`.

| Error | Meaning |
| --- | --- |
| `BackendBusy` | Contention or timeout. A retry can work |
| `BackendUnavailable` | Storage is not available. A retry can work |
| `BackendReadOnly` | A write went to read-only storage |
| `BackendCapacityExceeded` | Storage or a configured limit is full |
| `BackendCorruption` | Storage is corrupt or not valid |

## Stores and indexes

- **A store owns the source of truth,** and it can change it. `BlockStore` owns blocks, `LinkStore` owns links, and the optional structure capabilities own keyed, sequence, and temporal state.
- **An index is a read-only view** over that state. So `TraversalIndex` and `SearchIndex` have no write methods.

| Index | Derived from |
| --- | --- |
| `TraversalIndex` | `MemoryBlock.parent_id` in the block store |
| `SearchIndex` | Block state, plus link state for `Scope.LINKS` |

- **One database can be both.** The split tells who owns the data, not how you deploy it. When a single adapter holds both the stores and its own indexes, that adapter alone keeps them in step.

**A separate index adds 2 responsibilities:**

1. **Traversal defines scope.** `Memory` finds ownership only through `TraversalIndex`, and it never reads `parent_id` itself. So a separate traversal index decides subtree scope, traversal, and recursive delete, and it has to agree with the block store.
2. **A rebuild happens outside these contracts.** They have no method that lists all blocks. So a rebuild needs native storage access, or a set of root ids that the application keeps.

## Implementations

```text
lib/src/genie/backend/
  in_memory/reference.py
  relational/sqlite.py
  relational/postgres.py
  graph/neo4j.py
  document/mongodb.py
  key_value/redis.py
  composite/routed.py
  base.py
  capabilities.py
  structure_capabilities.py
```

| Group | Implementation | Guide |
| --- | --- | --- |
| In-memory | `InMemoryBackend` | [In-memory](in_memory/README.md) |
| Relational | `SQLiteBackend`, `PostgresBackend` | [Relational](relational/README.md) |
| Graph | `Neo4jBackend` | [Neo4j](graph/README.md) |
| Document | `MongoDBBackend` | [MongoDB](document/README.md) |
| Key-value | `RedisBackend` | [Redis](key_value/README.md) |
| Composite | `RoutedBackend` | [Composite](composite/README.md) |

- **Shared protocols stay at the package root.**
- **Not shipped yet:** columnar and time-series adapters.
- **Imports:** each bundled class is available from `genie` and from `genie.backend`.

## Small production deployments

The shipped server-backed and file-backed adapters suit small, controlled
production deployments. Each one gives you atomic writes, conflict handling,
schema checks, all the optional structure capabilities, and `RouteCatalog`.

| Backend | Good fit | Deployment responsibility |
| --- | --- | --- |
| `SQLiteBackend` | Local applications that do not write from many places at the same time | A reliable local filesystem, file access, and backups |
| `PostgresBackend` | Applications that already run a PostgreSQL server and need shared access | TLS, credentials, backups, monitoring, and capacity |
| `Neo4jBackend` | Applications that already run a managed Neo4j server | TLS, credentials, backups, monitoring, and capacity |
| `MongoDBBackend` | Applications that already run a MongoDB replica set and want document storage | The replica set, TLS, credentials, backups, monitoring, and capacity |
| `RedisBackend` | Applications that already run a single managed Redis server, not a Redis Cluster, and want low-latency shared memory | Persistence (RDB or AOF), `maxmemory-policy noeviction`, TLS, credentials, memory limits, and backups |

- **Writes go through the Genie backend,** not through direct edits of its stored records.
- **Known throughput limits:** `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` run writes one at a time to stay correct, so the second writer waits. `RedisBackend` and `MongoDBBackend` retry the writer that lost, and they do not block it. Portable search scans candidate blocks in both cases.
- **Past those limits,** a dedicated search implementation or a larger storage design is a better fit.

## Optional structure capabilities

| Capability | Required behavior |
| --- | --- |
| `AcyclicLinkStore` | Protect a relation only if its links form a DAG. Then check for cycles and insert the edge in one atomic step |
| `KeyedStore` | Unique bindings and compare-and-swap updates |
| `SequenceStore` | Allocate increasing sequence numbers atomically |
| `TemporalStore` | Temporal membership and stable paging |
| `TemporalIndex` | Optional prefiltering for temporal queries |

- **Not part of `MemoryBackend`.** An adapter implements only the capabilities that it supports.
- **A facade takes the store separately** from the access object. So it can bind to the adapter that holds the records.
- **`BlockRouter`** is the one capability that a composite backend adds. It tells which backend owns a block. When a facade gets a router, it resolves the block through the router before it checks for a structure capability.
- **Routing is not a claim about storage.** A composite that implements `BlockRouter` still advertises only what it implements itself.
- **`VersionedRecords`** combines `KeyedStore` and `SequenceStore`, and it adds no new backend method.
- **An implementation that is split internally** coordinates cleanup and transactions, so that no structure record points at nothing.

## Route catalog capability

| Capability | Required behavior |
| --- | --- |
| `RouteCatalog` | Claim one route for a block id in one atomic step, and reject a second claim |

- **Used by `RoutedBackend`,** through its `route_catalog` argument. Each composite instance over the same routes shares one catalog, so one block id cannot get 2 routes. Refer to [Composite](composite/README.md).
- **Entries are `RouteInfo` records, not blocks.** Block writes and deletes do not change them, so the backend of a route can also hold the catalog.
- **Not part of `MemoryBackend`.** An adapter implements it only if it can claim atomically.

## Adapter checklist

A production adapter does these things:

- **Rejects** duplicate ids and parent cycles.
- **Rejects non-string ids** with `TypeError` when the query language of the store reads another type as an operator. `Memory` and the record types already refuse them, but a caller can call an adapter directly. `MongoDBBackend` does this.
- **Keeps ownership correct,** and cleans up dependent state on delete.
- **Returns copies,** not internal objects, and pages results in a stable order.
- **Enforces atomically:** block versions, subtree snapshots, keyed updates, sequences, DAG inserts, and route catalog claims.
- **Documents what a retried commit repeats,** if it retries at all.
- **Maps operational failures** to `BackendError`.
- **Returns normalized `SearchResult` values** in the search order: `score` from high to low, then `created_at`, then block id. It returns only the results after `query.after`, and at most `query.limit`. `page_results` does this for a list of scored results.
- **Gives a block the same search score on each call,** and computes it from the block and the query only. A changing score makes a cursor skip or repeat results. A score from other blocks lets hidden blocks change the order of visible results.

The reusable [backend
contracts](../../../tests/backends/test_backend_contracts.py) cover the optional
capabilities and their behavior under concurrent writes.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
and [Queries](../query/README.md).
