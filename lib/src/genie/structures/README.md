# Structures

`genie.structures` gives optional views over blocks that already exist. A
facade adds organization and queries, and it does not change the values that
your application stored. One block can be in several structures at the same
time.

```python
from genie.structures import OwnershipTree

# Equivalent, because every public name is also exported from the root package:
from genie import OwnershipTree
```

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

- **Access object:** a facade takes a `Memory` or a `MemoryHandle`. A handle keeps its usual subtree and policy limits.
- **A membership change needs `write` on the container.** `bind`, `rebind`, `unbind`, `append`, `add`, `replace`, and `remove` call `require_access` on the access object before they touch the store. So a principal that can only read a container cannot change what it holds. A refusal gets to `on_denied` under the `access.require` operation.
- **A read needs `read` on the container and on each member.** An exact lookup raises `AccessDenied` for a member that the principal cannot read. A listing skips that member. `TemporalCollection.get` takes a block id, so it checks that block before the membership lookup. It also raises `AccessDenied` for an unreadable block that is not a member, so it does not show the membership of a hidden block.
- **`VersionedRecords` splits the rule.** `register` needs `write` on the collection block. `append` needs `write` on the identity block, because the identity is the anchor of the revision log.
- **Hook reuse:** key, log, and temporal changes through a facade use the hook registry, the principal, and the hook context attributes of the access object, if it has them. A `hooks=` argument replaces the registry. With no registry, the facade sends no structure events.

## Shared records

Immutable records describe membership by block id:

- **`MemoryAddress`, `KeyBinding`** — namespaced keys.
- **`LogEntry`** — a stable position in a log.
- **`ValidityInterval`, `TemporalEntry`** — time.
- **`GraphNeighbor`, `GraphPath`** — traversal.
- **`RevisionEntry`** — one revision of a logical record.

2 rules apply to them:

- **Records check their input, but they do not store anything.** The backend capabilities own the bindings, positions, and temporal entries.
- **2 different counters:** `MemoryBlock.version` counts the updates of one block. `RevisionEntry.revision` numbers the revisions of a record, from 1. It is the log sequence plus one, so a deleted revision leaves a gap.

## Membership is not ownership

- **Each keyed, log, and temporal facade has one ordinary block as its anchor, its container.** That is the block that you give when you build the facade: the `collection` for a `KeyedCollection`, or the `log` for an `EventLog`.
- **A new member gets a record that names it by id.** This does not change `MemoryBlock.parent_id`. The block keeps its current parent, and it does not become a child of the container.
- **Search and traversal follow stored parents, not membership records.** A search with the container as its root finds a member only when that member is also a child of the container.

This gives 2 choices:

| Choice | Container-scoped search | Lifecycle |
| --- | --- | --- |
| Create members as children of the container | Finds them | A delete of the container deletes them |
| Create members somewhere else | Does not find them | Another part of the tree owns them |

`PropertyGraph` and `LineageDAG` have a relation as their anchor, not a
container block. So this choice does not apply to them.

## Ownership and graphs

**`OwnershipTree`** is a rooted view of parent and child behavior:

```python
tree = OwnershipTree(handle, root=workspace)
task = tree.create_child(type="task", value="Draft proposal")
note = tree.create_child(parent=task, type="note", value="Keep it short")
```

- **It never goes above its `root`.**
- **A member check reads only the root and the member.** It calls `contains` on the access object. So the policies of other branches have no effect on it.
- **`descendants` skips a closed branch.** A block that denies `traverse_down` is in the result, but its descendants are not.
- **Ownership controls lifecycle.** So a delete of a parent deletes its descendants.

**`PropertyGraph`** treats blocks as nodes and `MemoryLink` values as directed,
typed edges:

```python
graph = PropertyGraph(handle)
graph.add_edge(note, proposal, "supports", metadata={"confidence": 0.9})
paths = graph.walk(note, relation="supports", max_depth=3)
```

- **Allows** cycles, self-links, and parallel edges.
- **Traversal has a depth limit** and uses the stored links.

**`LineageDAG`** sends its reads to that graph, but it keeps one relation free
of cycles:

```python
lineage = LineageDAG(handle, relation="derived_into")
lineage.add(source, summary)
```

- **The backend checks reachability and inserts the edge in one atomic step.** A read followed by a write is not safe when several writers run at the same time.
- **Protection covers the whole backend,** not one handle, subtree, or tenant. After the first `lineage.add`, a plain `add_link` with that relation raises `MemoryConflict` for all blocks. The protection stays, also after all links of the relation are deleted. `RoutedBackend` without a shared link store protects each route separately. See the [composite guide](../backend/composite/README.md#acyclic-links).
- **Only `LineageDAG` and `add_acyclic_link` write the relation safely.** Plain links that exist before the first lineage edge stay. If they form a DAG, the protection includes them. If they contain a cycle, each `lineage.add` raises `MemoryConflict`, and the relation stays without protection.
- **A prefixed relation name for each lineage graph,** such as `"billing.derived_from"`, makes sure that another component or tenant does not choose the same name by accident.

## Keys, logs, and time

```python
address = MemoryAddress(("users", "alice"), "language")
preferences = KeyedCollection(handle, backend, collection=preferences_root)
preferences.bind(address, language)

events = EventLog(handle, backend, log=events_root)
events.append(message)
recent = events.tail(limit=20)
```

- **`KeyedCollection.bind` only creates.** A `bind` on an address that is already bound raises `MemoryConflict`. `rebind` and `unbind` compare the block id that you expect, and they raise `MemoryConflict` if it is different.
- **Log sequence numbers are unique and always increase.** A delete of a block that a log refers to removes its entry, and the log does not use the sequence number again. A delete of the log block itself also removes its counter. So a new log block with the same id starts again at 0.

```python
timeline = TemporalCollection(handle, backend, collection=timeline_root)
timeline.add(
    fact,
    occurred_at=observed_at,
    validity=ValidityInterval(valid_at, invalid_at),
)
```

- **Intervals are half-open:** the start time is in the interval, and the end time is not. This applies to event ranges and to validity intervals.
- **`TemporalStore` gives stable scans in ascending block-id order,** not in time order. An optional `TemporalIndex` filters them first, and the facade checks each entry again.
- **Choice by axis:** `EventLog` for insertion order, `TemporalCollection` for event or validity time. One block can be in both.

## Versioned records

`VersionedRecords` combines a keyed identity with an event log:

```python
records = VersionedRecords(handle, backend, collection=records_root)
records.register(address, identity)
records.append(address, first_revision)
records.append(address, second_revision)
```

- **The address names a stable block,** and that block is the anchor of the ordered revision blocks.
- **Registration and the first append are separate calls,** because not all backends can run a transaction across 2 structures.
- **Revisions are immutable by convention,** as snapshots or deltas. Nothing enforces this. Trusted code can still update their blocks through the normal memory APIs.

## Backend capabilities and composition

| Capability | Used by |
| --- | --- |
| `AcyclicLinkStore` | `LineageDAG` writes |
| `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalStore` | `TemporalCollection` |
| `TemporalIndex` | Optional temporal optimization |

- **A capability that the store does not have raises `UnsupportedOperation`** when the facade is built. The exception is `LineageDAG`: it checks nothing when it is built, and its first `add` raises `UnsupportedOperation`.
- **The store is a separate argument from the access object,** because an access object does not expose a backend. A `MemoryHandle` hides its backend on purpose: with the backend, a caller can go around the scope of the handle.

### Composite backends

- **A composite such as `RoutedBackend` owns no structural state.** So it cannot hold records itself.
- **It implements `BlockRouter`.** As the store, it resolves to the route that owns the container block. A route is one named backend inside the composite.
- **An unknown container raises `MemoryNotFound`,** not `UnsupportedOperation`.
- **Structure membership stays inside the resolved route.** See the [composite guide](../backend/composite/README.md).
- **`LineageDAG` takes no store.** It writes through `Memory`. `RoutedBackend` implements `AcyclicLinkStore`: it sends each acyclic link to the store that holds its links.

### Composition

Facades store references, not copies. So an application can combine any subset
of them:

```python
@dataclass(frozen=True)
class AgentMemory:
    preconditions: KeyedCollection
    experiments: EventLog
    knowledge: PropertyGraph
```

`LineageDAG` reuses `PropertyGraph`. `VersionedRecords` reuses
`KeyedCollection` and `EventLog`. See the [advanced
examples](../../../../examples/advanced/README.md) and the [backend
contracts](../backend/README.md).
