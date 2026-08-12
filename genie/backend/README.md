# Backends

`genie.backend` separates the public memory API from storage and retrieval.
Applications give `Memory` one top-level backend, which may use one database or
coordinate several stores and indexes.

```python
memory = Memory(backend=MyBackend(...))
```

## Core contract

`MemoryBackend` combines four structurally typed protocols. An adapter does not
need to inherit from a Genie base class; it must provide the required behavior.

| Protocol | Responsibility |
| --- | --- |
| `BlockStore` | Create, read, conditionally update, and delete blocks |
| `TraversalIndex` | Parent/child traversal and containment |
| `SearchIndex` | Execute normalized `MemoryQuery` values |
| `LinkStore` | Persist and list typed links |

An adapter may implement every method itself or delegate internally to SQL,
graph, vector, document, or other components. `Memory` still receives one
backend object.

### Conditional writes

- `update_block(..., expected_version=...)` succeeds only when the stored
  version matches and the replacement increments it by one.
- `delete_block(..., expected_versions=...)` succeeds only when the mapping
  exactly matches the current subtree.

`Memory.set`, `Memory.update`, and `Memory.delete` supply these expectations
after authorization. Backends must check and mutate atomically. `None` permits
trusted backend-level administration to make an unconditional change.

### Errors

Use `MemoryConflict` for expected identity, version, or structure conflicts.
Map storage failures to `BackendError` subclasses and preserve the driver error
as `__cause__`:

| Error | Meaning |
| --- | --- |
| `BackendBusy` | Retryable contention or timeout |
| `BackendUnavailable` | Retryable storage unavailability |
| `BackendReadOnly` | Write attempted against read-only storage |
| `BackendCapacityExceeded` | Storage or configured limit exhausted |
| `BackendCorruption` | Corrupt or invalid storage |

## Stores and indexes

A store is the source of truth for blocks, links, and structural state. A
search or traversal index is an optimization and should be rebuildable. A
single database may serve both roles; the distinction describes ownership, not
a required deployment topology.

## Implementations

```text
genie/backend/
  in_memory/reference.py
  relational/sqlite.py
  composite/routed.py
  base.py
  capabilities.py
  structure_capabilities.py
```

| Group | Implementation | Guide |
| --- | --- | --- |
| In-memory | `InMemoryBackend` | [In-memory](in_memory/README.md) |
| Relational | `SQLiteBackend` | [Relational](relational/README.md) |
| Composite | `RoutedBackend` | [Composite](composite/README.md) |

Shared protocols stay at the package root. Genie does not yet bundle document,
key-value, columnar, graph, or time-series adapters. All bundled classes are
available from `genie` and `genie.backend`.

## Optional structure capabilities

| Capability | Required behavior |
| --- | --- |
| `AcyclicLinkStore` | Atomic cycle check and edge insertion |
| `KeyedStore` | Unique bindings and compare-and-swap updates |
| `SequenceStore` | Atomic monotonic sequence allocation |
| `TemporalStore` | Temporal membership and stable paging |
| `TemporalIndex` | Optional temporal query prefiltering |

These are not part of `MemoryBackend`; adapters implement only what they
support. `VersionedRecords` combines `KeyedStore` and `SequenceStore` without
adding another backend method. A split internal implementation must coordinate
cleanup and transactions so it cannot leave dangling structure records.

## Adapter checklist

A production adapter should:

- reject duplicate ids and invalid parent cycles;
- preserve ownership integrity and clean dependent state on deletion;
- return defensive values and stable paging order;
- atomically enforce block versions, subtree snapshots, keyed updates,
  sequences, and DAG insertion;
- map operational failures to `BackendError`; and
- return normalized `SearchResult` values.

Reusable [backend contracts](../../tests/structures/test_backend_contracts.py)
cover optional capability semantics and concurrent atomicity.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
and [Queries](../query/README.md).
