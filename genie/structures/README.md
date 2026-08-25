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

A facade takes either a `Memory` or a `MemoryHandle`. A handle keeps its usual
subtree and policy limits. Explicit key, log, and temporal changes reuse the
access object's hook registry, principal, and hook context attributes when those
are available.

## Shared records

Immutable records describe membership by block id:

- `MemoryAddress` and `KeyBinding`: namespaced keys;
- `LogEntry`: a stable position in a log;
- `ValidityInterval` and `TemporalEntry`: time;
- `GraphNeighbor` and `GraphPath`: traversal; and
- `RevisionEntry`: one revision of a logical record.

Records validate their input, but they do not store data. The backend
capabilities own the bindings, positions, and temporal entries.
`MemoryBlock.version` counts updates to one block. `RevisionEntry.revision` is
the position of a block inside a record's history.

Membership is not ownership. Each of these facades is anchored to one ordinary
block, called its container. It is the block you pass in when you build the
facade: the `collection` for a `KeyedCollection`, or the `log` for an
`EventLog`.

Adding a block to a collection, a log, or a timeline writes a record that names
that block by id. It does not change `MemoryBlock.parent_id`. So the block keeps
whatever parent it already had, and it does not become a child of the container.

This matters because search and traversal follow stored parents, not membership
records. A search rooted at the container finds a member only when that member
is also a child of the container. You have two choices:

- Create the members as children of the container. A container-scoped search
  then finds them. But ownership now controls their lifecycle too, so deleting
  the container deletes them.
- Create the members somewhere else. The structure then only refers to blocks
  that another part of the tree owns, and a container-scoped search does not
  find them.

`PropertyGraph` and `LineageDAG` are anchored to a relation instead of a
container block, so this choice does not apply to them.

## Ownership and graphs

`OwnershipTree` is a rooted view of parent/child behavior:

```python
tree = OwnershipTree(handle, root=workspace)
task = tree.create_child(type="task", value="Draft proposal")
note = tree.create_child(parent=task, type="note", value="Keep it short")
```

It never goes above its root. Ownership controls lifecycle, so deleting a parent
deletes its descendants.

`PropertyGraph` treats blocks as nodes and `MemoryLink` values as directed,
typed edges:

```python
graph = PropertyGraph(handle)
graph.add_edge(note, proposal, "supports", metadata={"confidence": 0.9})
paths = graph.walk(note, relation="supports", max_depth=3)
```

A property graph allows cycles, self-links, and parallel edges. Traversal has a
depth limit and reuses the stored links. `LineageDAG` passes its reads to this
graph, but it keeps one relation free of cycles:

```python
lineage = LineageDAG(handle, relation="derived_into")
lineage.add(source, summary)
```

The backend must check reachability and insert the edge in one atomic step.
Reading first and then writing is not safe when several writers run at once.

## Keys, logs, and time

```python
address = MemoryAddress(("users", "alice"), "language")
preferences = KeyedCollection(handle, backend, collection=preferences_root)
preferences.bind(address, language)

events = EventLog(handle, backend, log=events_root)
events.append(message)
recent = events.tail(limit=20)
```

Key `bind` only creates. `rebind` and `unbind` compare the block id you expect.
Log sequence numbers are unique and always increase. Deleting a block that a log
refers to removes its entry, and the sequence number is not reused.

```python
timeline = TemporalCollection(handle, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=observed_at,
    validity=ValidityInterval(valid_at, invalid_at),
)
```

Event ranges and validity intervals are half-open. `TemporalStore` gives stable
scans, and an optional `TemporalIndex` prefilters them. Use `EventLog` for
insertion order and `TemporalCollection` for event or validity time. One block
may belong to both.

## Versioned records

`VersionedRecords` combines a keyed identity with an event log:

```python
records = VersionedRecords(handle, backend, collection=records_root)
records.register(address, identity)
records.append(address, first_revision)
records.append(address, second_revision)
```

The address names a stable block, and that block anchors the ordered revision
blocks. Registration and the first append are separate calls because not every
backend can run a transaction across two structures. Treat revisions as
immutable snapshots or deltas. Trusted code can still update their blocks
through the normal memory APIs.

## Backend capabilities and composition

| Capability | Used by |
| --- | --- |
| `AcyclicLinkStore` | `LineageDAG` writes |
| `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalStore` | `TemporalCollection` |
| `TemporalIndex` | Optional temporal optimization |

Building a facade on a backend that lacks its capability raises
`UnsupportedOperation`. A facade takes the store separately from the access
object because an access object does not expose a backend. A `MemoryHandle`
hides its backend on purpose: revealing it would let a caller step around the
handle's scope.

A composite backend such as `RoutedBackend` owns no structural state, so it
cannot hold records itself. But it does implement `BlockRouter`, so passing it
as the store resolves to the route that owns the container block. An unknown
container raises `MemoryNotFound` instead of reporting a missing capability.
Structure membership stays inside the resolved route. See the [composite
guide](../backend/composite/README.md).

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
