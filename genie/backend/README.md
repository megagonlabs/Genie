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

- `update_block(..., expected_version=...)` succeeds only when the stored
  version matches. The replacement must raise the version by one.
- `delete_block(..., expected_versions=...)` succeeds only when the mapping
  matches the current subtree exactly.

`Memory.set`, `Memory.update`, and `Memory.delete` pass these expectations
after they authorize the request. A backend must check and change state in one
atomic step, which means the check and the change happen together so that no
other writer can slip in between them. Passing `None` lets trusted
backend-level administration make an unconditional change.

### Errors

Raise `MemoryConflict` for expected conflicts of identity, version, or
structure. Map storage failures to a `BackendError` subclass, and keep the
original driver error as `__cause__`:

| Error | Meaning |
| --- | --- |
| `BackendBusy` | Contention or timeout. Retrying may work |
| `BackendUnavailable` | Storage is unavailable. Retrying may work |
| `BackendReadOnly` | A write was attempted against read-only storage |
| `BackendCapacityExceeded` | Storage or a configured limit is full |
| `BackendCorruption` | Storage is corrupt or invalid |

## Stores and indexes

A store owns the source of truth and can change it. `BlockStore` owns blocks,
`LinkStore` owns links, and the optional structure capabilities own keyed,
sequence, and temporal state. `TraversalIndex` and `SearchIndex` are read-only
views over that state, so they have no write methods.

| Index | Derived from |
| --- | --- |
| `TraversalIndex` | `MemoryBlock.parent_id` in the block store |
| `SearchIndex` | Block state, plus link state for `Scope.LINKS` |

One database may act as both a store and an index. The split describes who owns
the data, not how you must deploy it. When a single adapter holds both the
stores and its own indexes, keeping them in step is that adapter's own business.

Supplying an index separately adds two responsibilities. First, `Memory` works
out ownership only through `TraversalIndex` and never reads `parent_id` itself.
So a supplied traversal index decides subtree scope, traversal, and recursive
deletion, and it must agree with the block store. Second, these contracts have
no method that lists every block. Rebuilding an index from the stores is
therefore a job you do outside these contracts. It needs native storage access,
or a set of root ids that the application keeps.

## Implementations

```text
genie/backend/
  in_memory/reference.py
  relational/sqlite.py
  graph/neo4j.py
  composite/routed.py
  base.py
  capabilities.py
  structure_capabilities.py
```

| Group | Implementation | Guide |
| --- | --- | --- |
| In-memory | `InMemoryBackend` | [In-memory](in_memory/README.md) |
| Relational | `SQLiteBackend` | [Relational](relational/README.md) |
| Graph | `Neo4jBackend` | [Neo4j](graph/README.md) |
| Composite | `RoutedBackend` | [Composite](composite/README.md) |

The shared protocols stay at the package root. Genie does not yet ship document,
key-value, columnar, or time-series adapters. Every bundled class can be
imported from `genie` and from `genie.backend`.

## Small production deployments

`SQLiteBackend` and `Neo4jBackend` are suitable for small, controlled production
deployments. Both give you atomic writes, conflict handling, schema checks, and
all the optional structure capabilities.

| Backend | Good fit | Deployment responsibility |
| --- | --- | --- |
| `SQLiteBackend` | Local applications that do not write from many places at once | Use a reliable local filesystem, and manage file access and backups |
| `Neo4jBackend` | Applications that already run a managed Neo4j server | Configure TLS, credentials, backups, monitoring, and capacity |

Write through the Genie backend instead of editing its stored records directly.
Neo4j writes run one at a time to stay correct, and portable search scans
candidate blocks. When your dataset or your traffic grows past these limits, use
a dedicated search implementation or a larger storage design.

Genie is still an alpha library. Data safety in a backend and stability of the
public API are two separate questions.

## Optional structure capabilities

| Capability | Required behavior |
| --- | --- |
| `AcyclicLinkStore` | Check for cycles and insert the edge in one atomic step |
| `KeyedStore` | Unique bindings and compare-and-swap updates |
| `SequenceStore` | Allocate increasing sequence numbers atomically |
| `TemporalStore` | Temporal membership and stable paging |
| `TemporalIndex` | Optional prefiltering for temporal queries |

These are not part of `MemoryBackend`. An adapter implements only the ones it
supports. A structure facade takes the store separately from the access object,
so it can bind to whichever adapter holds the records.

`BlockRouter` is the one capability that a composite backend adds. It reports
which backend owns a block. When a facade is given a router, it resolves the
block through the router before it checks for a structure capability. Routing is
not a claim about storage: a composite that implements `BlockRouter` still
advertises only what it implements itself. `VersionedRecords` combines
`KeyedStore` and `SequenceStore` and adds no new backend method. An
implementation that is split internally must coordinate cleanup and transactions
so that it never leaves structure records pointing at nothing.

## Adapter checklist

A production adapter should:

- reject duplicate ids and parent cycles;
- keep ownership correct, and clean up dependent state on deletion;
- return copies rather than internal objects, and page results in a stable
  order;
- atomically enforce block versions, subtree snapshots, keyed updates,
  sequences, and DAG insertion;
- map operational failures to `BackendError`; and
- return normalized `SearchResult` values.

The reusable [backend
contracts](../../tests/structures/test_backend_contracts.py) cover the optional
capabilities and their behavior under concurrent writes.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
and [Queries](../query/README.md).
