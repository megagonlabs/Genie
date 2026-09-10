# Backends

`genie.backend` keeps the public memory API separate from storage and
retrieval. An application gives `Memory` one top-level backend. That backend may
use a single database, or it may coordinate several stores and indexes.

```python
memory = Memory(backend=MyBackend(...))
```

## Core contract

`MemoryBackend` is made of four protocols that are matched by structure. An
adapter does not have to inherit from a Genie base class. It only has to provide
the required behavior.

| Protocol | Responsibility |
| --- | --- |
| `BlockStore` | Create, read, conditionally update, and delete blocks |
| `TraversalIndex` | Parent/child traversal and containment |
| `SearchIndex` | Run normalized `MemoryQuery` values |
| `LinkStore` | Store and list typed links |

An adapter may implement every method itself, or pass the work to SQL, graph,
vector, document, or other components inside it. Either way, `Memory` still
receives one backend object.

### Conditional writes

- **`update_block(..., expected_version=...)`** succeeds only when the stored version matches. The replacement must raise the version by one.
- **`delete_block(..., expected_versions=...)`** succeeds only when the mapping matches the current subtree exactly.
- **`Memory.set`, `Memory.update`, and `Memory.delete`** pass these expectations after they authorize the request.
- **Atomicity is required.** A backend must check and change state in one atomic step, which means the check and the change happen together so that no other writer can slip in between them.
- **`None` skips the check,** which lets trusted backend-level administration make an unconditional change.

### Retried commits

- **A lost reply is indistinguishable from a lost commit.** An adapter that talks to a server over a network can lose the reply to a commit the server already applied, so a retry may run the same operation a second time.
- **Write every operation so a repeat is safe:** either rejected by its own uniqueness check, or ending at the same state.
- **`append_log_entry` is the exception.** It has no uniqueness key, so a repeat appends the same block again at a new sequence.

### Errors

- **`MemoryConflict`** for expected conflicts of identity, version, or structure.
- **A `BackendError` subclass** for storage failures. Keep the original driver error as `__cause__`.

| Error | Meaning |
| --- | --- |
| `BackendBusy` | Contention or timeout. Retrying may work |
| `BackendUnavailable` | Storage is unavailable. Retrying may work |
| `BackendReadOnly` | A write was attempted against read-only storage |
| `BackendCapacityExceeded` | Storage or a configured limit is full |
| `BackendCorruption` | Storage is corrupt or invalid |

## Stores and indexes

- **A store owns the source of truth** and can change it: `BlockStore` owns blocks, `LinkStore` owns links, and the optional structure capabilities own keyed, sequence, and temporal state.
- **An index is a read-only view** over that state, so `TraversalIndex` and `SearchIndex` have no write methods.

| Index | Derived from |
| --- | --- |
| `TraversalIndex` | `MemoryBlock.parent_id` in the block store |
| `SearchIndex` | Block state, plus link state for `Scope.LINKS` |

- **One database may act as both.** The split describes who owns the data, not how you must deploy it. When a single adapter holds both the stores and its own indexes, keeping them in step is that adapter's own business.

**Supplying an index separately adds two responsibilities:**

1. **Traversal defines scope.** `Memory` works out ownership only through `TraversalIndex` and never reads `parent_id` itself. So a supplied traversal index decides subtree scope, traversal, and recursive deletion, and it must agree with the block store.
2. **Rebuilding happens outside these contracts.** They have no method that lists every block, so a rebuild needs native storage access, or a set of root ids that the application keeps.

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
- **Imports:** every bundled class can be imported from `genie` and from `genie.backend`.

## Small production deployments

The shipped server-backed and file-backed adapters are suitable for small,
controlled production deployments. Each one gives you atomic writes, conflict
handling, schema checks, and all the optional structure capabilities.

| Backend | Good fit | Deployment responsibility |
| --- | --- | --- |
| `SQLiteBackend` | Local applications that do not write from many places at once | Use a reliable local filesystem, and manage file access and backups |
| `PostgresBackend` | Applications that already run a PostgreSQL server and need shared access | Configure TLS, credentials, backups, monitoring, and capacity |
| `Neo4jBackend` | Applications that already run a managed Neo4j server | Configure TLS, credentials, backups, monitoring, and capacity |
| `MongoDBBackend` | Applications that already run a MongoDB replica set and want document storage | Configure the replica set, TLS, credentials, backups, monitoring, and capacity |
| `RedisBackend` | Applications that already run a single managed Redis server, not a Redis Cluster, and want low-latency shared memory | Configure persistence (RDB or AOF), `maxmemory-policy noeviction`, TLS, credentials, memory limits, and backups |

- **Write through the Genie backend** instead of editing its stored records directly.
- **Known throughput limits:** `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` run writes one at a time to stay correct, so the second writer waits. `RedisBackend` and `MongoDBBackend` retry the writer that lost instead of blocking it. Portable search scans candidate blocks either way.
- **Past those limits,** use a dedicated search implementation or a larger storage design.
- **Alpha status is a separate question.** Data safety in a backend and stability of the public API are not the same thing.

## Optional structure capabilities

| Capability | Required behavior |
| --- | --- |
| `AcyclicLinkStore` | Check for cycles and insert the edge in one atomic step |
| `KeyedStore` | Unique bindings and compare-and-swap updates |
| `SequenceStore` | Allocate increasing sequence numbers atomically |
| `TemporalStore` | Temporal membership and stable paging |
| `TemporalIndex` | Optional prefiltering for temporal queries |

- **Not part of `MemoryBackend`.** An adapter implements only the ones it supports.
- **A facade takes the store separately** from the access object, so it can bind to whichever adapter holds the records.
- **`BlockRouter`** is the one capability a composite backend adds. It reports which backend owns a block. When a facade is given a router, it resolves the block through the router before it checks for a structure capability.
- **Routing is not a claim about storage.** A composite that implements `BlockRouter` still advertises only what it implements itself.
- **`VersionedRecords`** combines `KeyedStore` and `SequenceStore` and adds no new backend method.
- **An internally split implementation** must coordinate cleanup and transactions so that it never leaves structure records pointing at nothing.

## Adapter checklist

A production adapter should:

- **Reject** duplicate ids and parent cycles.
- **Keep ownership correct,** and clean up dependent state on deletion.
- **Return copies** rather than internal objects, and page results in a stable order.
- **Enforce atomically:** block versions, subtree snapshots, keyed updates, sequences, and DAG insertion.
- **Document what a retried commit repeats,** if it retries at all.
- **Map operational failures** to `BackendError`.
- **Return normalized `SearchResult` values.**

The reusable [backend
contracts](../../../tests/backends/test_backend_contracts.py) cover the optional
capabilities and their behavior under concurrent writes.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
and [Queries](../query/README.md).
