# Data Model

The records Genie stores, how they point at each other, and the rules that keep
them consistent. This is a logical model: it fixes the shape of each record, not
a storage layout, and not what a value means. Field-level detail for
`MemoryBlock` and `MemoryLink` is in
[Detailed architecture](04-detailed-architecture.md#stored-shapes).

## A worked example

A billing support desk has one finding and three customer tickets. The finding
explains the tickets, and it stayed true from 3 March until deploy `D-88` fixed
the problem on 20 March.

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

What is stored, and what each part answers:

| Question | Record that answers it | Result |
| --- | --- | --- |
| **What belongs to the desk?** | `parent_id` on each block | `finding`, `ticket`, `ticket`, `ticket` |
| **Which tickets does the finding explain?** | 3 `MemoryLink` rows, relation `"explains"` | `T-4471`, `T-4482`, `T-4490` |
| **Which block is ticket `T-4482`?** | `KeyBinding` at `("tickets",)` / `"T-4482"` | that ticket's block id |
| **In what order did tickets arrive?** | `LogEntry` rows in the desk's log | sequences `0`, `1`, `2` |
| **Was the finding true on 10 March?** | `TemporalEntry` with a `ValidityInterval` | yes |
| **Was it true on 25 March?** | same entry | empty result |

- **Every structure points at the same blocks by id.** After
  `memory.set(finding, "Refund emails take 3 days. Fixed by deploy D-88.")`,
  the finding is at `version` `2`, and the graph, the key lookup, and
  `time.valid_at(...)` all return the new text. Nothing was copied.
- **Delete removes the dependent records.** After `memory.delete` on ticket
  `T-4482`, its key binding is gone, the finding has 2 links left, and the log
  reads sequences `0` and `2`. The gap at `1` stays; sequences are never reused.
- **Deleting the desk deletes everything it owns,** and the key bindings, log,
  and temporal entries anchored to it go with it.
- **Genie gives no meaning to `"desk"`, `"ticket"`, `"explains"`, or the value
  shapes.** They are the application's choice.

## Records

| Record | Identity | Points at | Stored by | Used by |
| --- | --- | --- | --- | --- |
| `MemoryBlock` | `id` (`mem_` prefix) | its parent, through `parent_id` | `BlockStore` | `Memory`, `OwnershipTree` |
| `MemoryLink` | `id` (`link_` prefix) | `source_id`, `target_id` | `LinkStore` (`AcyclicLinkStore` for DAG relations) | `Memory`, `PropertyGraph`, `LineageDAG` |
| `MemoryPolicy` | none; a value inside a block | nothing | inside `MemoryBlock.policy` | `PolicyEngine` |
| `KeyBinding` | `(collection_id, address)` | `block_id` | `KeyedStore` | `KeyedCollection`, `VersionedRecords` |
| `LogEntry` | `(log_id, sequence)` | `block_id` | `SequenceStore` | `EventLog`, `VersionedRecords` |
| `TemporalEntry` | `(collection_id, block_id)` | `block_id` | `TemporalStore` | `TemporalCollection` |

- **`MemoryBlock` is the only record that holds a value.** Every other record
  holds ids and a small amount of its own data: a relation name, an address, a
  sequence, or a time.
- **Every record is a frozen dataclass with `slots=True`.** A change creates a
  new object.
- **The container fields (`collection_id`, `log_id`) are block ids.** Each
  keyed collection, log, and temporal collection is anchored to one ordinary
  block, so no separate "collection" record exists.
- **`RevisionEntry` is not stored.** `VersionedRecords` builds it from a
  `KeyBinding` (the identity) and a `LogEntry` (the position), with
  `revision = sequence + 1`.
- **`GraphPath`, `GraphNeighbor`, `KeyedItem`, `LogItem`, `TemporalItem`, and
  `RevisionItem` are read results,** not stored records. Each pairs a record
  with the visible `MemoryBlock` it names, and checks that both use the same
  block id.

### `MemoryBlock`

- **`type` and `value`** are application-defined. `value` is stored encoded;
  `content_type` (default `application/json`) selects the codec that decodes
  it.
- **`parent_id`** is the only ownership field. It is set at creation, and no
  public `Memory` call changes it.
- **`metadata`** is always a strict JSON dict, whatever the value codec is.
  It is used for filters and adapter indexing.
- **`policy`** is set at creation and never changes. `None` is read as
  `MemoryPolicy.owner_private()`.
- **`created_by`** is the principal that created the block. `AccessRule` uses
  it as the owner.
- **`created_at`, `updated_at`** are timezone-aware UTC.
- **`version`** starts at `1` and rises by exactly `1` per update. Updates
  compare it (`expected_version`), so a stale writer gets `MemoryConflict`.

### `MemoryLink`

- **Fields:** `source_id`, `target_id`, `relation`, `id`, `metadata`,
  `created_at`. No version and no update time.
- **Directed and typed.** `relation` is a free string. Parallel links between
  the same two blocks are allowed and stay separate records.
- **Only `metadata` changes,** by merge, through `Memory.update_link`.
- **`LinkRelations.MOUNT` (`"genie.mount"`) is reserved** for mounts, and
  `add_acyclic_link` refuses it.

### `MemoryPolicy`

- **One `AccessRule` per built-in operation:** `read`, `write`, `search`,
  `traverse_up`, `traverse_down`.
- **`operations`** holds extra named rules, such as `"export"`. They cannot
  reuse a built-in name.
- **An `AccessRule`** allows everyone (`allow_all`), or only the listed
  `principals`, `roles`, and optionally the owner (`allow_owner`).

### Structure records

| Record | Fields | Unique by | Order in listings |
| --- | --- | --- | --- |
| `KeyBinding` | `collection_id`, `address`, `block_id` | `(collection_id, address)` | ascending address |
| `LogEntry` | `log_id`, `sequence`, `block_id` | `(log_id, sequence)` | ascending sequence, or descending with `reverse=True` |
| `TemporalEntry` | `collection_id`, `block_id`, `occurred_at`, `validity` | `(collection_id, block_id)` | ascending block id |

- **`MemoryAddress`** is `namespace` (a tuple of non-empty strings; `()` is the
  collection root) plus a non-empty `key`.
- **One block can have many keys.** Uniqueness is on the address, not on the
  block.
- **One block can appear in a log many times,** at different sequences. An
  append has no uniqueness key, so a retried commit can add a duplicate.
- **A `TemporalEntry` has `occurred_at`, `validity`, or both.** Event time and
  validity are separate: an event happens once, while a fact can stay true for
  a period.
- **`ValidityInterval` is half-open:** it includes `valid_at` and excludes
  `invalid_at`. A missing `invalid_at` means open-ended. All times are
  timezone-aware and normalized to UTC.

## Relationships

| Kind | Stored as | Subtree scope and search | Recursive delete | Access |
| --- | --- | --- | --- | --- |
| **Ownership** | `MemoryBlock.parent_id` | follows it | follows it | no inheritance |
| **Link** | `MemoryLink` | reached only through `Scope.LINKS` or graph facades | not followed | no inheritance |
| **Mount** | `MemoryLink` with `"genie.mount"` | not followed | not followed | no inheritance |
| **Membership** | `KeyBinding`, `LogEntry`, `TemporalEntry` | not followed | not followed | no inheritance |

- **Ownership forms a forest.** Each block has one parent or none, and a block
  cannot move under itself or its own descendant.
- **Membership is not ownership.** Binding a ticket in a `KeyedCollection` does
  not change its `parent_id`. A search rooted at the collection block finds the
  ticket only if it is also a real child of that block, as in the example.
- **Policies never inherit.** Each block is checked against its own policy.
  The one implied power is that `write` on a block authorizes deleting its
  whole subtree.
- **A mount points at another memory root.** `resolve_mount` reads the target
  through a normal `get`, so the target's own policy applies.

## Identity and counters

| Counter | Scope | Starts at | Meaning |
| --- | --- | --- | --- |
| `MemoryBlock.version` | one block | `1` | number of updates to that block |
| `LogEntry.sequence` | one log | `0` | position of an append; never reused |
| `RevisionEntry.revision` | one versioned record | `1` | position of a block in that record's history |

- **Ids are prefixed by kind:** `mem_` for blocks, `link_` for links, plus a
  `uuid4` hex. A caller may supply its own block id.
- **`version` and `revision` are different things.** `version` counts updates
  to one stored block. `revision` counts separate blocks kept under one logical
  record, where each revision block is left unchanged.

## Integrity rules

- **References are block ids.** Link endpoints are checked to exist when the
  link is created. Structure facades check that the block is visible through
  the caller's access object before they write a record.
- **Delete cascades to every dependent record.** `BlockStore.delete_block`
  removes the subtree, and in the same atomic step every link touching those
  blocks and every key binding, log entry, and temporal entry that names them,
  either as the container or as the target.
- **A stale subtree fails the delete.** The caller passes the `{id: version}`
  snapshot it authorized, and the backend raises `MemoryConflict` if a block
  was added, removed, or changed since.
- **Atomic steps live in the store.** Binding a used address, closing a cycle
  in an acyclic relation, adding a block twice to one temporal collection, and
  allocating a log sequence are checked inside one store call.
- **A dangling record is an integrity error.** An exact structure lookup that
  finds a record for a missing block raises `MemoryNotFound`. Paged readers
  instead skip blocks the caller cannot see.

## Storage mapping

- **No physical schema is defined.** Each adapter maps these records onto its
  own tables, documents, nodes, or keys, and records a schema version for the
  layout it creates.
- **Stores are the source of truth.** `BlockStore`, `LinkStore`, and the
  structure stores hold the state and are the only parts that write.
- **`TraversalIndex` and `SearchIndex` are derived.** They expose reads only
  and can be rebuilt from the stores. `Memory` still answers every ownership
  question through `TraversalIndex`, not by reading `parent_id` itself.
- **Values, metadata, and policies are stored as JSON** where the store has no
  native type, and rebuilt into `MemoryPolicy` and `AccessRule` objects on
  read.
- **One logical backend can span several databases.** `RoutedBackend` keeps
  each block and its subtree on one route, with optional shared link,
  traversal, and search components.

## Left out on purpose

- **No domain schema.** Genie defines no block types, value shapes, or
  relation names beyond `"genie.mount"`.
- **No cross-record transactions.** Each public operation is one backend
  write. Composing several operations into one unit is application work.
- **No policy change and no reparenting** through the public API.
- **No stored revision table.** Revisions are derived from keys and log
  entries.

## Related reading

- [Overview](01-overview.md) — the model and the reasoning behind it
- [High-level architecture](03-high-level-architecture.md) — components and boundaries
- [Detailed architecture](04-detailed-architecture.md) — field tables, delete order, and backend internals
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md)
