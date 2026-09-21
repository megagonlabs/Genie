# High-Level Architecture

The parts of Genie, what each one owns, and how one call travels through them.
Module internals and algorithms belong in
[Detailed architecture](04-detailed-architecture.md).

## Shape of the system

- **Genie is a library, not a service.** It has no server, no daemon, no
  background threads, and no scheduler. It runs inside the application process
  that imports it.
- **Every public call is synchronous** and returns before the next one starts.
  Hooks run on the calling thread too.
- **The application owns the process and the database.** Genie holds no
  connection pool of its own, starts nothing, and shuts nothing down.
- **State lives in the backend.** A `Memory` object holds configuration
  (backend, principal, policy engine, hooks, codecs, default policy,
  attributes) and no block data, so two `Memory` objects over one backend see
  the same blocks.
- **The core imports nothing outside the standard library.** Third-party
  drivers arrive only with the extra that needs them, and only inside the
  adapter that uses them.

## Layers

```text
 Application or agent tools
              |
      Structure facades     optional views over the same blocks
              |
        MemoryHandle        optional root, principal, and capability limits
              |
           Memory           policy, hooks, codecs, and operation flow
              |
        MemoryBackend       blocks, ownership, links, and search
              |
     Stores and indexes     chosen by the backend
```

| Layer | Owns | Required |
| --- | --- | --- |
| Structure facades | Reference records that arrange existing blocks into a tree, graph, key map, log, timeline, DAG, or revision chain | No |
| `MemoryHandle` | A root block, an acting principal, and a set of `HandleCapability` limits | No |
| `Memory` | Operation flow, policy checks, hook dispatch, codec encode and decode | Yes |
| `MemoryBackend` | Block storage, ownership traversal, links, and search behind one protocol | Yes |
| Stores and indexes | The actual database, file, or process memory, and any index over it | Chosen by the backend |

- **Two layers are enough to run Genie:** a `Memory` object and one backend
  object. Everything above `Memory` is optional.
- **Handles and facades are views, not copies.** They read and write the same
  blocks through the same `Memory`, so adding one never moves data.
- **One backend object may hide several databases.** `Memory` sees a single
  `MemoryBackend`; `RoutedBackend` is what turns that single object into
  several stores behind application-defined routes.
- **The layers below `Memory` never call back up.** A backend does not know
  about handles, facades, hooks, or policies.

## Components

| Package | Responsibility | Main public names |
| --- | --- | --- |
| `genie.memory` | The entry point, the stored model, and scoped access | `Memory`, `MemoryBlock`, `MemoryLink`, `MemoryHandle`, the error types |
| `genie.policy` | Who may act on a block, and the check that decides it | `MemoryPolicy`, `AccessRule`, `Principal`, `PolicyEngine`, `Operation` |
| `genie.query` | A portable way to describe a search and its results | `MemoryQuery`, `Scope`, `SearchResult` |
| `genie.hooks` | Observation and controlled customization of each operation | `MemoryHook`, `HookRegistry`, `HookContext`, the event payloads |
| `genie.codecs` | Value translation at the storage boundary | `MemoryCodec`, `CodecRegistry`, `JsonCodec` |
| `genie.structures` | Optional facades and the immutable records they return | `OwnershipTree`, `PropertyGraph`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, `VersionedRecords` |
| `genie.backend` | The storage contracts and the shipped adapters | `MemoryBackend`, the capability protocols, `InMemoryBackend`, `SQLiteBackend`, and the optional adapters |

- **`Memory` is the only component that coordinates the others.** It is the
  single place where a policy check, a hook call, a codec call, and a backend
  call meet.
- **`genie.codecs` depends on no other Genie package.** It is a standalone
  translation layer, which is why an application can register its own codec
  without touching anything else.
- **`genie.backend` never imports `genie.structures`.** Structure facades
  depend on backend capability protocols, not the other way round.

## Dependency direction

```text
policy.principal -> policy.policy -> memory.block -> memory.links
                                          |              |
                                          +-> query -----+
                                                 |
                                       hooks ----+---- backend contracts
                                                 |
                                    memory.Memory / MemoryHandle
                                                 |
                                         structure facades
```

- **Dependencies point one way.** The shared model modules
  (`policy/principal.py`, `policy/policy.py`, `memory/block.py`,
  `memory/links.py`, `query/`) import nothing above them, so every other layer
  can speak in those types without a cycle.
- **Facades sit at the top.** They may import `memory`, `policy`, `query`,
  `hooks`, and `backend`. Nothing imports them back.
- **A facade depends on the narrowest protocol it needs,** not on `Memory`,
  `MemoryHandle`, or a backend class. `OwnershipTree` takes `OwnershipAccess`
  (`BlockReader` plus `TreeReader` plus `put` and `delete`), and
  `PropertyGraph` takes `GraphAccess` (`BlockReader` plus `LinkReader` and
  `LinkWriter`). Both `Memory` and `MemoryHandle` satisfy them.
- **`LineageDAG` and `VersionedRecords` are composed, not new storage.**
  `LineageDAG` wraps `PropertyGraph`; `VersionedRecords` wraps
  `KeyedCollection` and `EventLog`.

## The path of one operation

### `memory.put(...)`

A create call through a trusted `Memory` object:

1. **Open the operation context.** `Memory` opens one `HookContext` carrying
   the acting principal, the start time, and the memory's attributes.
2. **Authorize the parent, if there is one.** The parent block is read from
   the backend, then checked for `WRITE` and `TRAVERSE_DOWN`. A refusal is
   reported to `on_denied` and raised as `AccessDenied`.
3. **Encode the value.** `CodecRegistry.encode` turns the application value
   into storage data plus a content type.
4. **Build the block.** Id, type, encoded value, parent id, metadata, policy
   (the explicit argument or `Memory.default_policy`), and creator.
5. **Call `before_put`.** Hooks may replace the block, including its policy.
   Changing `parent_id` is rejected, so a hook cannot move the block into
   someone else's subtree after the parent check has already passed.
6. **Write once.** `backend.put_block` stores the block.
7. **Call `after_put` and decode.** The stored block is reported, then decoded
   and returned with the application value.

### `handle.search(...)`

A search through a bounded handle shows the two checks working together:

1. **The handle narrows the query.** It applies its own root, its default
   scope, and its capability limits before anything reaches `Memory`. A scope
   the handle does not allow is refused here.
2. **`Memory` authorizes the query root.** `SEARCH` on the root block, plus
   `TRAVERSE_DOWN`, `TRAVERSE_UP`, or `READ` depending on the scope.
3. **`before_search` may rewrite the query.** The rewritten query is
   normalized and authorized again, so a hook cannot widen the root or the
   scope.
4. **The backend searches** and applies `query.limit`.
5. **`Memory` filters the results** against block policies and re-checks the
   query scope, then decodes the surviving blocks.
6. **`after_search` may change the list,** and the same filter runs a second
   time over whatever the hooks returned. Verdicts are cached per block id, so
   a block is read once for both passes.

- **The limit is a candidate budget, not a result count.** Filtering happens
  after the backend has already spent the limit, so a visible result page can
  be shorter than the limit the caller asked for.
- **A filtered read returns less, it does not refuse.** The caller learns
  nothing about the blocks it cannot see.
- **Which right each operation checks** is listed in
  [Detailed architecture](04-detailed-architecture.md#authorization-per-operation).

## Boundaries

| Boundary | Enforced by | What crosses it |
| --- | --- | --- |
| Public API | `genie.__init__` | Every exported name; nothing else is a supported import |
| Access | `MemoryHandle` plus `MemoryPolicy` | Only blocks inside the handle root whose policy allows the acting principal |
| Storage | `MemoryBackend` and the capability protocols | Encoded `MemoryBlock` and `MemoryLink` values, `MemoryQuery`, `SearchResult` |
| Value | `MemoryCodec` | The application value in, storage data plus a content type out |
| Observation | `HookRegistry` | `HookContext` and typed event payloads, never raw backend objects |

- **The trust boundary sits under the handle.** `Memory`, backends, the policy
  engine, the hook registry, and the codec registry belong in trusted
  application code. Agents get a rooted `MemoryHandle`, or a smaller tool the
  application writes over one.
- **Raw backend access skips everything.** Calling a backend directly bypasses
  handles, policies, hooks, and codecs. That is expected, and it is the reason
  the backend object stays in trusted code.
- **Child handles only narrow.** A child cannot change principal, widen the
  root, or restore a capability its parent disabled.

## Extension seams

| Seam | Contract | Default | Typical reason to replace it |
| --- | --- | --- | --- |
| Storage | `MemoryBackend` | `InMemoryBackend` | Use a real database, or route blocks across several |
| Access rules | `PolicyEngine` and `MemoryPolicy` | `PolicyEngine`, `owner_private()` | Express an ownership or sharing rule the built-in rules do not cover |
| Observation | `MemoryHook` in a `HookRegistry` | Empty registry | Audit trails, metrics, validation, enrichment at `before_put` |
| Value translation | `MemoryCodec` in a `CodecRegistry` | `JsonCodec` | Store a value type JSON cannot carry |
| Organization | Structure facades | None | Arrange existing blocks without a new storage model |

- **Every seam is a protocol or a small class, not a plugin system.** There is
  no registry file, no entry point, and no discovery step. The application
  passes the object to the `Memory` constructor.
- **Optional backend capabilities are separate protocols.** `AcyclicLinkStore`,
  `KeyedStore`, `SequenceStore`, `TemporalStore`, and `TemporalIndex` sit
  beside the core contract, so an adapter can ship without them and a facade
  can check for the one it needs.
- **A missing capability raises `UnsupportedOperation`,** which is also what a
  shipped adapter raises when its driver is not installed.

## How facades attach

- **Facades take an access object,** either a `Memory` or a `MemoryHandle`.
  Every block read and write still goes through it, so scope and policy checks
  are the same as a direct call.
- **Facades that keep records take a store as well.** `KeyedCollection`,
  `EventLog`, `TemporalCollection`, and `VersionedRecords` need a backend
  capability for their bindings, entries, and sequences. `OwnershipTree`,
  `PropertyGraph`, and `LineageDAG` need no extra store because parents and
  links already carry their state.
- **A routing backend resolves to one route.** When the store argument is a
  `BlockRouter`, the facade binds to the backend that owns the anchoring
  block. A container and its subtree always share one route.
- **Membership is not ownership.** Key bindings, log entries, temporal
  entries, and revisions point at blocks by id and never change a stored
  parent, so subtree search reaches a member only when the application also
  created it under the container.

## Cross-cutting concerns

- **Access control** is two independent checks: the handle scope and the block
  policy. Both must allow the operation. Policies do not inherit through
  ownership, and a policy is fixed when the block is created.
- **Observability** runs through hooks. Every operation that starts reports
  exactly one outcome event, and every refusal reports its subject and the
  check that produced it, so a filtered result is auditable instead of silent.
- **Consistency** is optimistic. Block updates carry an expected version,
  recursive deletion sends the exact subtree snapshot it observed, and a
  concurrent change raises `MemoryConflict` rather than committing stale work.
- **Atomic behavior belongs to the backend.** When an operation cannot be a
  safe portable read followed by a write, it becomes a backend capability with
  contract and concurrency tests, not a best-effort sequence in a facade.
- **Errors are one family.** Everything Genie raises derives from
  `GenieError`: `MemoryNotFound`, `MemoryConflict`, `UnsupportedOperation`,
  `HookRecursionError`, and the `BackendError` group that adapters map storage
  failures onto.

## Left out on purpose

- **No process of its own:** no server, no worker, no background job, no
  retry loop, no cache layer.
- **No embeddings and no ranking policy.** Search is a backend capability, and
  how a backend scores a match is its own decision.
- **No admin override and no policy mutation.** Authority stays where the
  creator put it.
- **No policy inheritance through ownership.** `write` on a block is authority
  over the existence of what it owns, and that is the only implied power.
- **No memory taxonomy.** Block types, value shapes, retrieval strategy, and
  agent behavior stay in the application.

## Related reading

- [Overview](01-overview.md) — the model and the reasoning behind it
- [Detailed architecture](04-detailed-architecture.md) — module internals
- [Repository organization](07-repo-organization.md) — where each component lives
- [Memory and handles](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md) — the detail for one area
- [Queries](../lib/src/genie/query/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
