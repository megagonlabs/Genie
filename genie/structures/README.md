# Structures

The `genie.structures` package provides optional views over existing memory
blocks. Facades add organization and query behavior without changing the
meaning of block values.

One block can participate in several structures at the same time.

## Available facades

| Facade | Purpose | Storage used |
| --- | --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy | Block parent ids |
| `PropertyGraph` | Typed relationships and graph traversal | Memory links |
| `KeyedCollection` | Namespaced key lookup | Key bindings |
| `EventLog` | Stable append order | Log entries |
| `TemporalCollection` | Event time and validity queries | Temporal entries |
| `LineageDAG` | Cycle-protected derivation graph | Memory links |
| `VersionedRecords` | Stable identity and revision history | Key bindings and log entries |

Facades receive a `Memory` or `MemoryHandle` for block access. When given a
handle, they keep its subtree and policy restrictions.

## Shared records

Immutable records describe structural membership by block id:

- `MemoryAddress` and `KeyBinding` describe namespaced keys;
- `LogEntry` describes a stable position in a log;
- `ValidityInterval` and `TemporalEntry` describe time;
- `GraphNeighbor` and `GraphPath` describe traversal; and
- `RevisionEntry` describes a logical record revision.

Records validate data but do not persist it. Backend capabilities own the
stored bindings, positions, and temporal entries.

`MemoryBlock.version` and `RevisionEntry.revision` are different. The first
counts updates to one block; the second is a position in a record's history.

## Ownership tree

`OwnershipTree` is a rooted view of existing parent/child behavior. It creates
children, performs bounded traversal, and delegates deletion to normal memory
operations.

```python
tree = OwnershipTree(handle, root=workspace)
task = tree.create_child(type="task", value="Draft proposal")
note = tree.create_child(parent=task, type="note", value="Keep it short")
```

The facade never traverses above its root. Parent/child ownership controls
lifecycle; deleting a parent deletes its descendants.

## Property graph and lineage

`PropertyGraph` treats blocks as nodes and `MemoryLink` values as directed,
typed edges:

```python
graph = PropertyGraph(handle)
graph.add_edge(note, proposal, "supports", metadata={"confidence": 0.9})
paths = graph.walk(note, relation="supports", max_depth=3)
```

Property graphs may contain cycles, self-links, and parallel edges. Traversal
is bounded and does not create another graph representation.

`LineageDAG` reuses the property graph but protects one exact relation from
cycles:

```python
lineage = LineageDAG(handle, relation="derived_into")
lineage.add(source, summary)
```

The backend must check reachability and insert the edge atomically. A portable
read-then-write check is unsafe under concurrent writers.

## Keyed collections and event logs

`KeyedCollection` binds a namespaced address to an existing block:

```python
address = MemoryAddress(("users", "alice"), "language")
preferences = KeyedCollection(handle, backend, collection=preferences_root)
preferences.bind(address, language)
```

`bind` is create-only. `rebind` and `unbind` use an expected block id so the
backend can apply compare-and-swap semantics.

`EventLog` appends references to existing blocks:

```python
events = EventLog(handle, backend, log=events_root)
entry = events.append(message)
recent = events.tail(limit=20)
```

Sequence numbers are unique and increase within one log. Deleting an entry's
block removes that entry but does not reuse its sequence number.

## Temporal collections

`TemporalCollection` stores event time and validity separately:

```python
timeline = TemporalCollection(handle, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=observed_at,
    validity=ValidityInterval(valid_at, invalid_at),
)
```

Event ranges and validity intervals are half-open. A required
`TemporalStore` provides stable paged scans. An optional `TemporalIndex` may
prefilter queries; without it, the facade applies filters portably.

Use `EventLog` when insertion order matters. Use `TemporalCollection` when
event or validity time matters. A block can belong to both.

## Versioned records

`VersionedRecords` composes `KeyedCollection` and `EventLog`:

```python
records = VersionedRecords(handle, backend, collection=records_root)
records.register(address, identity)
records.append(address, first_revision)
records.append(address, second_revision)
```

The address maps to a stable identity block. That identity anchors an ordered
log of revision blocks. Registration and the first append are separate because
not every backend can provide one transaction across both operations.

Treat revision blocks as immutable snapshots or deltas. Genie preserves their
identity and order but does not prevent trusted code from updating them through
the normal memory API.

## Backend capabilities

The optional structures use small, runtime-checkable protocols:

| Capability | Used by |
| --- | --- |
| `AcyclicLinkStore` | `LineageDAG` writes |
| `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalStore` | `TemporalCollection` |
| `TemporalIndex` | Optional temporal query optimization |

Backends implement only the capabilities they support. Unsupported facade
construction raises `UnsupportedOperation`.

## Composition

Facades store references instead of copying block values. This allows one
compound memory to expose only the structures an agent needs:

```python
@dataclass(frozen=True)
class AgentMemory:
    preconditions: KeyedCollection
    experiments: EventLog
    knowledge: PropertyGraph
```

`LineageDAG` delegates reads and traversal to `PropertyGraph`.
`VersionedRecords` delegates identity lookup to `KeyedCollection` and revision
ordering to `EventLog`.

See the runnable [advanced examples](../../examples/advanced/README.md) for one
structure, a subset, and all seven structures together. Backend authors should
also run the reusable contracts described in the
[backend guide](../backend/README.md).
