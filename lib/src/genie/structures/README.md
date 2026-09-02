# Structures

`genie.structures` provides optional views over blocks that already exist. A
facade adds organization and queries without changing the values your
application stored, and one block may take part in several structures at once.

## Facades

| Facade | Purpose | Stored structure |
| --- | --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy | Parent ids |
| `PropertyGraph` | Typed graph traversal | Links |
| `KeyedCollection` | Namespaced key lookup | Key bindings |
| `EventLog` | Stable append order | Log entries |
| `TemporalCollection` | Event and validity time | Temporal entries |
| `LineageDAG` | Derivation, protected from cycles | Links |
| `VersionedRecords` | Stable identity and revisions | Keys and log entries |

- **Access object:** a facade takes either a `Memory` or a `MemoryHandle`. A handle keeps its usual subtree and policy limits.
- **Hook reuse:** explicit key, log, and temporal changes reuse the access object's hook registry, principal, and hook context attributes when those are available.

## Shared records

Immutable records describe membership by block id:

- **`MemoryAddress`, `KeyBinding`** — namespaced keys.
- **`LogEntry`** — a stable position in a log.
- **`ValidityInterval`, `TemporalEntry`** — time.
- **`GraphNeighbor`, `GraphPath`** — traversal.
- **`RevisionEntry`** — one revision of a logical record.

Two rules about them:

- **Records validate, they do not store.** The backend capabilities own the bindings, positions, and temporal entries.
- **Two different counters:** `MemoryBlock.version` counts updates to one block, while `RevisionEntry.revision` is the position of a block inside a record's history.

## Membership is not ownership

- **Every keyed, log, and temporal facade is anchored to one ordinary block, its container.** That is the block you pass in when you build the facade: the `collection` for a `KeyedCollection`, or the `log` for an `EventLog`.
- **Adding a block writes a record that names it by id.** It does not change `MemoryBlock.parent_id`. The block keeps whatever parent it already had, and it does not become a child of the container.
- **Search and traversal follow stored parents, not membership records.** A search rooted at the container finds a member only when that member is also a child of the container.

Two choices follow:

| Choice | Container-scoped search | Lifecycle |
| --- | --- | --- |
| Create members as children of the container | Finds them | Deleting the container deletes them |
| Create members somewhere else | Does not find them | Another part of the tree owns them |

`PropertyGraph` and `LineageDAG` are anchored to a relation instead of a
container block, so this choice does not apply to them.

## Ownership and graphs

**`OwnershipTree`** is a rooted view of parent/child behavior:

```python
tree = OwnershipTree(handle, root=workspace)
task = tree.create_child(type="task", value="Draft proposal")
note = tree.create_child(parent=task, type="note", value="Keep it short")
```

- **It never goes above its `root`.**
- **Ownership controls lifecycle,** so deleting a parent deletes its descendants.

**`PropertyGraph`** treats blocks as nodes and `MemoryLink` values as directed,
typed edges:

```python
graph = PropertyGraph(handle)
graph.add_edge(note, proposal, "supports", metadata={"confidence": 0.9})
paths = graph.walk(note, relation="supports", max_depth=3)
```

- **Allows** cycles, self-links, and parallel edges.
- **Traversal has a depth limit** and reuses the stored links.

**`LineageDAG`** passes its reads to that graph, but keeps one relation free of
cycles:

```python
lineage = LineageDAG(handle, relation="derived_into")
lineage.add(source, summary)
```

- **The backend must check reachability and insert the edge in one atomic step.** Reading first and then writing is not safe when several writers run at once.

## Keys, logs, and time

```python
address = MemoryAddress(("users", "alice"), "language")
preferences = KeyedCollection(handle, backend, collection=preferences_root)
preferences.bind(address, language)

events = EventLog(handle, backend, log=events_root)
events.append(message)
recent = events.tail(limit=20)
```

- **`KeyedCollection.bind` only creates.** `rebind` and `unbind` compare the block id you expect.
- **Log sequence numbers are unique and always increase.** Deleting a block that a log refers to removes its entry, and the sequence number is not reused.

```python
timeline = TemporalCollection(handle, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=observed_at,
    validity=ValidityInterval(valid_at, invalid_at),
)
```

- **Intervals are half-open:** the start time is included and the end time is not. This covers both event ranges and validity intervals.
- **`TemporalStore` gives stable scans,** and an optional `TemporalIndex` prefilters them.
- **Pick by axis:** `EventLog` for insertion order, `TemporalCollection` for event or validity time. One block may belong to both.

## Versioned records

`VersionedRecords` combines a keyed identity with an event log:

```python
records = VersionedRecords(handle, backend, collection=records_root)
records.register(address, identity)
records.append(address, first_revision)
records.append(address, second_revision)
```

- **The address names a stable block,** and that block anchors the ordered revision blocks.
- **Registration and the first append are separate calls,** because not every backend can run a transaction across two structures.
- **Treat revisions as immutable** snapshots or deltas. Trusted code can still update their blocks through the normal memory APIs.

## Backend capabilities and composition

| Capability | Used by |
| --- | --- |
| `AcyclicLinkStore` | `LineageDAG` writes |
| `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalStore` | `TemporalCollection` |
| `TemporalIndex` | Optional temporal optimization |

- **A missing capability raises `UnsupportedOperation`** when the facade is built.
- **The store is passed separately from the access object,** because an access object does not expose a backend. A `MemoryHandle` hides its backend on purpose: revealing it would let a caller step around the handle's scope.

### Composite backends

- **A composite such as `RoutedBackend` owns no structural state,** so it cannot hold records itself.
- **It does implement `BlockRouter`.** Passing it as the store resolves to the route that owns the container block. A route is one named backend inside the composite.
- **An unknown container raises `MemoryNotFound`** instead of reporting a missing capability.
- **Structure membership stays inside the resolved route.** See the [composite guide](../backend/composite/README.md).

### Composition

Facades store references rather than copies, so an application can combine any
subset of them:

```python
@dataclass(frozen=True)
class AgentMemory:
    preconditions: KeyedCollection
    experiments: EventLog
    knowledge: PropertyGraph
```

`LineageDAG` reuses `PropertyGraph`. `VersionedRecords` reuses
`KeyedCollection` and `EventLog`. See the [advanced
examples](../../examples/advanced/README.md) and the [backend
contracts](../backend/README.md).
