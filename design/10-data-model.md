# Data Model

This note describes the records that Genie stores, how the records refer to
each other, and the rules that keep them consistent. It is a logical model. It
sets the shape of each record. It does not set a storage layout or the meaning
of a value. For the fields of `MemoryBlock` and `MemoryLink`, see
[Detailed architecture](04-detailed-architecture.md#stored-shapes).

## A worked example

A billing support desk has 1 finding and 3 customer tickets. The finding
explains the tickets. The finding was true from 3 March until deploy `D-88`
fixed the problem on 20 March.

```python
from datetime import UTC, datetime

from genie import (
    EventLog, InMemoryBackend, KeyedCollection, Memory, MemoryAddress,
    PropertyGraph, TemporalCollection, ValidityInterval,
)

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="service:support")

desk = memory.put(type="desk", value={"team": "Billing support"})
finding = memory.put(parent=desk, type="finding", value="Refund emails take 3 days to arrive.")
tickets = [
    memory.put(parent=desk, type="ticket", value={"number": number, "subject": subject})
    for number, subject in [
        ("T-4471", "Where is my refund?"),
        ("T-4482", "Refund not received"),
        ("T-4490", "No refund email"),
    ]
]

graph = PropertyGraph(memory)
keys = KeyedCollection(memory, backend, collection=desk)
log = EventLog(memory, backend, log=desk)
for ticket in tickets:
    graph.add_edge(finding, ticket, "explains")
    keys.bind(MemoryAddress(("tickets",), ticket.value["number"]), ticket)
    log.append(ticket)

time = TemporalCollection(memory, backend, collection=desk)
time.add(finding, validity=ValidityInterval(
    datetime(2026, 3, 3, tzinfo=UTC), datetime(2026, 3, 20, tzinfo=UTC),
))
```

The table shows the stored records and the question that each record answers:

| Question | Record that answers it | Result |
| --- | --- | --- |
| **What belongs to the desk?** | `parent_id` on each block | `finding`, `ticket`, `ticket`, `ticket` |
| **Which tickets does the finding explain?** | 3 `MemoryLink` rows, relation `"explains"` | `T-4471`, `T-4482`, `T-4490` |
| **Which block is ticket `T-4482`?** | `KeyBinding` at `("tickets",)` / `"T-4482"` | the block id of that ticket |
| **In what order did tickets arrive?** | `LogEntry` rows in the log of the desk | sequences `0`, `1`, `2` |
| **Was the finding true on 10 March?** | `TemporalEntry` with a `ValidityInterval` | yes |
| **Was it true on 25 March?** | the same entry | an empty result |

- **All structures refer to the same blocks by id.** After
  `memory.set(finding, "Refund emails take 3 days. Fixed by deploy D-88.")`,
  the finding has `version` `2`. The graph, the key lookup, and
  `time.valid_at(...)` all return the new text. No structure holds a copy of
  the value.
- **A delete removes the dependent records.** After `memory.delete` on ticket
  `T-4482`, the key binding of that ticket does not exist. The finding has 2
  links. The log shows sequences `0` and `2`. The gap at `1` stays, because a
  log never uses a sequence again.
- **A delete of the desk also deletes all the blocks that it owns.** It also
  removes the key bindings, the log, and the temporal entries that use the desk
  as their container.
- **Genie gives no meaning to `"desk"`, `"ticket"`, `"explains"`, or the value
  shapes.** The application chooses them.

## Records

| Record | Identity | Refers to | Stored by | Used by |
| --- | --- | --- | --- | --- |
| `MemoryBlock` | `id` (`mem_` prefix) | its parent, through `parent_id` | `BlockStore` | `Memory`, `OwnershipTree` |
| `MemoryLink` | `id` (`link_` prefix) | `source_id`, `target_id` | `LinkStore` (`AcyclicLinkStore` for DAG relations) | `Memory`, `PropertyGraph`, `LineageDAG` |
| `MemoryPolicy` | none (a value inside a block) | nothing | inside `MemoryBlock.policy` | `PolicyEngine` |
| `KeyBinding` | `(collection_id, address)` | `block_id` | `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `LogEntry` | `(log_id, sequence)` | `block_id` | `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalEntry` | `(collection_id, block_id)` | `block_id` | `TemporalStore` | `TemporalCollection` |

- **`MemoryBlock` is the only record that holds a value.** All other records
  hold ids and a small quantity of their own data: a relation name, an address,
  a sequence, or a time.
- **Each record is a frozen dataclass with `slots=True`.** A change makes a new
  object.
- **The container fields (`collection_id`, `log_id`) are block ids.** Each
  keyed collection, log, and temporal collection uses one ordinary block as its
  container. So Genie has no separate "collection" record.
- **Genie does not store `RevisionEntry`.** `VersionedRecords` builds it from a
  `KeyBinding` (the identity) and a `LogEntry` (the position), with
  `revision = sequence + 1`.
- **`GraphPath`, `GraphNeighbor`, `KeyedItem`, `LogItem`, `TemporalItem`, and
  `RevisionItem` are read results,** not stored records. Each result pairs a
  record with the visible `MemoryBlock` that the record names. Each result also
  checks that both use the same block id.

### `MemoryBlock`

- **`id` and `type`** are non-empty strings. `parent_id` is also a non-empty
  string if it has a value.
- **`type` and `value`** are application-defined. The block stores `value` in
  encoded form. `content_type` (default `application/json`) selects the codec
  that decodes it.
- **`parent_id`** is the only ownership field. `Memory` sets it at creation. No
  public `Memory` call changes it.
- **`metadata`** is always a strict JSON dict, for all value codecs. Its
  maximum depth is `MAX_NESTING_DEPTH` (100) levels, and the dict itself is the
  first level. Filters and adapter indexes use it.
- **`policy`** gets its value at creation and never changes. Genie reads `None`
  as `MemoryPolicy.owner_private()`.
- **`created_by`** is the principal that created the block. `AccessRule` uses
  it as the owner.
- **`created_at`, `updated_at`** are timezone-aware UTC times.
- **`version`** starts at `1` and increases by exactly `1` for each update. An
  update compares it with `expected_version`, so a writer with a stale version
  gets `MemoryConflict`.

### `MemoryLink`

- **Fields:** `source_id`, `target_id`, `relation`, `id`, `metadata`,
  `created_at`. A link has no version and no update time.
- **Directed and typed.** `relation` is a free, non-empty string. Genie accepts
  parallel links between the same 2 blocks, and keeps them as separate records.
- **Only `metadata` changes.** `Memory.update_link` merges new metadata into
  it.
- **`LinkRelations.MOUNT` (`"genie.mount"`) is reserved for mounts.**
  `add_acyclic_link` refuses it.

### `MemoryPolicy`

- **One `AccessRule` for each built-in operation:** `read`, `write`, `search`,
  `traverse_up`, `traverse_down`.
- **`operations`** holds more named rules, for example `"export"`. These rules
  cannot use a built-in name.
- **An `AccessRule`** gives access to all principals (`allow_all`). Or it gives
  access only to the listed `principals` and `roles`, and, as an option, to the
  owner (`allow_owner`).
- **The defaults fail closed.** The default of `allow_all` is `False`, and
  `allow_all` cannot go together with the other grants. If a built-in
  `MemoryPolicy` field has no value, that field is `AccessRule.owner()`.

### Structure records

| Record | Fields | Unique by | Order in listings |
| --- | --- | --- | --- |
| `KeyBinding` | `collection_id`, `address`, `block_id` | `(collection_id, address)` | ascending address |
| `LogEntry` | `log_id`, `sequence`, `block_id` | `(log_id, sequence)` | ascending sequence, or descending with `reverse=True` |
| `TemporalEntry` | `collection_id`, `block_id`, `occurred_at`, `validity` | `(collection_id, block_id)` | ascending block id |

- **`MemoryAddress`** has a `namespace` and a non-empty `key`. The `namespace`
  is a tuple of non-empty strings, and `()` is the collection root.
- **One block can have many keys.** The address is unique, not the block.
- **One block can occur in a log many times,** at different sequences. An
  append has no uniqueness key. So if the application tries a commit again, the
  second try can add a duplicate entry.
- **A `TemporalEntry` has `occurred_at`, `validity`, or both.** Event time and
  validity are separate values. An event occurs one time. A fact can stay true for
  a period.
- **`ValidityInterval` is half-open:** it includes `valid_at` and does not
  include `invalid_at`. If `invalid_at` has no value, the interval has no end.
  All times are timezone-aware, and Genie normalizes them to UTC.

## Relationships

| Kind | Stored as | Subtree scope and search | Recursive delete | Access |
| --- | --- | --- | --- | --- |
| **Ownership** | `MemoryBlock.parent_id` | follows it | follows it | no inheritance |
| **Link** | `MemoryLink` | only through `Scope.LINKS` or graph facades | not followed | no inheritance |
| **Mount** | `MemoryLink` with `"genie.mount"` | not followed | not followed | no inheritance |
| **Membership** | `KeyBinding`, `LogEntry`, `TemporalEntry` | not followed | not followed | no inheritance |

- **Ownership makes a forest.** Each block has 1 parent or no parent. A block
  cannot move under itself or under its own descendant.
- **Membership is not ownership.** A key binding for a ticket in a
  `KeyedCollection` does not change the `parent_id` of the ticket. A search
  with the collection block as its root finds the ticket only if the ticket is
  also a real child of that block. The example shows this case.
- **Policies never inherit.** Genie checks each block against its own policy.
  There is 1 implied permission: `write` on a block authorizes a delete of its
  full subtree.
- **A mount refers to another memory root.** `resolve_mount` reads the target
  through a normal `get`, so the policy of the target applies.

## Identity and counters

| Counter | Scope | Starts at | Meaning |
| --- | --- | --- | --- |
| `MemoryBlock.version` | one block | `1` | counts the updates to that block |
| `LogEntry.sequence` | one log | `0` | position of an append, never used again |
| `RevisionEntry.revision` | one versioned record | `1` | position of a block in the history of that record |

- **Each id starts with a prefix for its kind:** `mem_` for blocks and `link_`
  for links. A `uuid4` hex follows the prefix. A caller can supply its own
  block id.
- **`version` and `revision` are different counters.** `version` counts the
  updates to one stored block. `revision` counts the separate blocks under one
  logical record. Each revision block stays unchanged.

## Integrity rules

- **References are block ids.** When a caller creates a link, Genie checks that
  both endpoints exist. Structure facades check that the block is visible
  through the access object of the caller before they write a record.
- **A delete cascades to each dependent record.** `BlockStore.delete_block`
  removes the subtree. In the same atomic step, it removes each link to or from
  those blocks. It also removes each key binding, log entry, and temporal entry
  that names those blocks as the container or as the target.
- **A stale subtree stops the delete.** The caller gives the `{id: version}`
  snapshot that it authorized. The backend raises `MemoryConflict` if a block
  was added, removed, or changed after that snapshot.
- **The store does the atomic steps.** The store checks these cases inside one
  store call. It refuses a bind to an address that is in use. It refuses a link
  that closes a cycle in an acyclic relation. It refuses a second add of a block
  to one temporal collection. It also allocates a log sequence inside one store
  call.
- **A dangling record is an integrity error.** If an exact structure lookup
  finds a record for a block that does not exist, it raises `MemoryNotFound`.
  In contrast, paged readers skip the blocks that the caller cannot see.

## Storage mapping

- **Genie defines no physical schema.** Each adapter maps these records to its
  own tables, documents, nodes, or keys. Each adapter also records a schema
  version for the layout that it makes.
- **Stores are the source of truth.** `BlockStore`, `LinkStore`, and the
  structure stores hold the state. They are the only parts that write.
- **`TraversalIndex` and `SearchIndex` are derived.** They supply reads only,
  and an adapter can build them again from the stores. But `Memory` answers
  each ownership question through `TraversalIndex`. It does not read
  `parent_id` directly.
- **Adapters store values, metadata, and policies as JSON** if the store has no
  native type for them. On a read, the adapter builds `MemoryPolicy` and
  `AccessRule` objects again from that JSON.
- **One logical backend can use several databases.** `RoutedBackend` keeps each
  block and its subtree on one route. As an option, it can also use shared
  link, traversal, and search components.

## Left out on purpose

- **No domain schema.** Genie defines no block types, value shapes, or relation
  names other than `"genie.mount"`.
- **No cross-record transactions.** Each public operation is one backend write.
  If an application needs several operations in one unit, the application does
  that work.
- **No policy change and no parent change** through the public API.
- **No stored revision table.** `VersionedRecords` derives revisions from keys
  and log entries.

## Related reading

- [Overview](01-overview.md) — the model and the reasons for it
- [High-level architecture](03-high-level-architecture.md) — components and boundaries
- [Detailed architecture](04-detailed-architecture.md) — field tables, delete order, and backend internals
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md)
