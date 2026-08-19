# Structures

`genie.structures` provides optional views over existing blocks. Facades add
organization and queries without changing application values, and one block
may participate in several structures.

## Facades

| Facade | Purpose | Stored structure |
| --- | --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy | Parent ids |
| `PropertyGraph` | Typed graph traversal | Links |
| `KeyedCollection` | Namespaced key lookup | Key bindings |
| `EventLog` | Stable append order | Log entries |
| `TemporalCollection` | Event and validity time | Temporal entries |
| `LineageDAG` | Cycle-protected derivation | Links |
| `VersionedRecords` | Stable identity and revisions | Keys and log entries |

Facades receive `Memory` or `MemoryHandle`; a handle keeps its normal subtree
and policy restrictions. Explicit key, log, and temporal mutations reuse the
access object's hook registry, principal, and hook context attributes when
available.

## Shared records

Immutable records describe membership by block id:

- `MemoryAddress` and `KeyBinding`: namespaced keys;
- `LogEntry`: a stable log position;
- `ValidityInterval` and `TemporalEntry`: time;
- `GraphNeighbor` and `GraphPath`: traversal; and
- `RevisionEntry`: a logical record revision.

Records validate but do not persist data. Backend capabilities own bindings,
positions, and temporal entries. `MemoryBlock.version` counts updates to one
block; `RevisionEntry.revision` is its position in a record history.

## Ownership and graphs

`OwnershipTree` is a rooted view of parent/child behavior:

```python
tree = OwnershipTree(handle, root=workspace)
task = tree.create_child(type="task", value="Draft proposal")
note = tree.create_child(parent=task, type="note", value="Keep it short")
```

It never traverses above its root. Ownership controls lifecycle, so deleting a
parent deletes descendants.

`PropertyGraph` treats blocks as nodes and `MemoryLink` values as directed,
typed edges:

```python
graph = PropertyGraph(handle)
graph.add_edge(note, proposal, "supports", metadata={"confidence": 0.9})
paths = graph.walk(note, relation="supports", max_depth=3)
```

Property graphs allow cycles, self-links, and parallel edges. Traversal is
bounded and reuses stored links. `LineageDAG` delegates reads to this graph but
protects one relation from cycles:

```python
lineage = LineageDAG(handle, relation="derived_into")
lineage.add(source, summary)
```

The backend must atomically check reachability and insert; a read-then-write
cycle check is unsafe under concurrent writers.

## Keys, logs, and time

```python
address = MemoryAddress(("users", "alice"), "language")
preferences = KeyedCollection(handle, backend, collection=preferences_root)
preferences.bind(address, language)

events = EventLog(handle, backend, log=events_root)
events.append(message)
recent = events.tail(limit=20)
```

Key `bind` is create-only; `rebind` and `unbind` compare the expected block id.
Log sequences are unique and increasing. Deleting a referenced block removes
its entry without reusing the sequence.

```python
timeline = TemporalCollection(handle, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=observed_at,
    validity=ValidityInterval(valid_at, invalid_at),
)
```

Event ranges and validity intervals are half-open. `TemporalStore` provides
stable scans; optional `TemporalIndex` prefilters them. Use `EventLog` for
insertion order and `TemporalCollection` for event or validity time. A block
may belong to both.

## Versioned records

`VersionedRecords` composes keyed identity with an event log:

```python
records = VersionedRecords(handle, backend, collection=records_root)
records.register(address, identity)
records.append(address, first_revision)
records.append(address, second_revision)
```

The address identifies a stable block that anchors ordered revision blocks.
Registration and first append are separate because not every backend supports
cross-structure transactions. Treat revisions as immutable snapshots or
deltas; trusted code can still update their blocks through normal memory APIs.

## Backend capabilities and composition

| Capability | Used by |
| --- | --- |
| `AcyclicLinkStore` | `LineageDAG` writes |
| `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalStore` | `TemporalCollection` |
| `TemporalIndex` | Optional temporal optimization |

Unsupported construction raises `UnsupportedOperation`. Facades store
references rather than copies, so applications can combine any subset:

```python
@dataclass(frozen=True)
class AgentMemory:
    preconditions: KeyedCollection
    experiments: EventLog
    knowledge: PropertyGraph
```

`LineageDAG` reuses `PropertyGraph`; `VersionedRecords` reuses
`KeyedCollection` and `EventLog`. See the [advanced
examples](../../examples/advanced/README.md) and [backend
contracts](../backend/README.md).
