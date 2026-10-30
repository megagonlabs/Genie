# High-Level Architecture

This note describes the parts of Genie, what each part owns, and the path of one
call through them. [Detailed architecture](04-detailed-architecture.md)
describes module internals and algorithms.

## Shape of the system

- **Genie is a library, not a service.** It has no server, no daemon, no
  background threads, and no scheduler. It runs inside the application process
  that imports it.
- **Each public call is synchronous.** It returns before the next call starts.
  Hooks also run on the thread of the caller.
- **The application owns the process and the database.** Genie has no
  connection pool of its own. It starts nothing and stops nothing.
- **State is in the backend.** A `Memory` object holds configuration and no
  block data. The configuration is the backend, principal, policy engine,
  hooks, codecs, default policy, and attributes. So 2 `Memory` objects over one
  backend see the same blocks.
- **The core imports nothing outside the standard library.** A third-party
  driver comes only with the extra that needs it. Only the adapter that uses
  the driver imports it.

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
| Structure facades | Reference records that arrange blocks into a tree, a graph, a key map, a log, a timeline, a DAG, or a revision chain | No |
| `MemoryHandle` | A root block, the principal that acts, and a set of `HandleCapability` limits | No |
| `Memory` | Operation flow, policy checks, hook dispatch, codec encode and decode | Yes |
| `MemoryBackend` | Block storage, ownership traversal, links, and search behind one protocol | Yes |
| Stores and indexes | The actual database, file, or process memory, and any index over it | Chosen by the backend |

- **Genie runs with 2 layers:** a `Memory` object and one backend object. All
  layers above `Memory` are optional.
- **Handles and facades are views, not copies.** They read and write the same
  blocks through the same `Memory`. So a new handle or facade never moves data.
- **One backend object can hide several databases.** `Memory` sees a single
  `MemoryBackend`. `RoutedBackend` turns that single object into several stores
  behind application-defined routes.
- **The layers below `Memory` never call a higher layer.** A backend does not
  know about handles, facades, hooks, or policies.

## Components

| Package | Responsibility | Main public names |
| --- | --- | --- |
| `genie.memory` | The entry point, the stored model, and scoped access | `Memory`, `MemoryBlock`, `MemoryLink`, `MemoryHandle`, the error types |
| `genie.policy` | Who can act on a block, and the check that decides it | `MemoryPolicy`, `AccessRule`, `Principal`, `PolicyEngine`, `Operation` |
| `genie.query` | A portable description of a search and its results | `MemoryQuery`, `Scope`, `SearchResult` |
| `genie.hooks` | Observation and controlled customization of each operation | `MemoryHook`, `HookRegistry`, `HookContext`, the event payloads |
| `genie.codecs` | Value translation at the storage boundary | `MemoryCodec`, `CodecRegistry`, `JsonCodec` |
| `genie.structures` | Optional facades and the immutable records that they return | `OwnershipTree`, `PropertyGraph`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, `VersionedRecords` |
| `genie.backend` | The storage contracts and the shipped adapters | `MemoryBackend`, the capability protocols, `InMemoryBackend`, `SQLiteBackend`, and the optional adapters |

- **`Memory` is the only component that coordinates the others.** It is the
  single place where a policy check, a hook call, a codec call, and a backend
  call meet.
- **`genie.codecs` depends on no other Genie package.** It is a standalone
  translation layer. So an application can register its own codec and change
  nothing else.
- **`genie.backend` never imports `genie.structures`.** Structure facades
  depend on backend capability protocols. The backend does not depend on the
  facades.

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

- **Dependencies go in one direction.** The shared model modules import
  nothing above them. These modules are `policy/principal.py`,
  `policy/policy.py`, `memory/block.py`, `memory/links.py`, and `query/`. So
  all other layers can use those types without an import cycle.
- **Facades are at the top.** They can import `memory`, `policy`, `query`,
  `hooks`, and `backend`. None of those packages imports the facades.
- **A facade depends on the narrowest protocol that it needs.** It does not
  depend on `Memory`, `MemoryHandle`, or a backend class.
  - **`OwnershipTree` takes `OwnershipAccess`.** This protocol is
    `BlockReader` plus `TreeReader` plus `put` and `delete`.
  - **`PropertyGraph` takes `GraphAccess`.** This protocol is `BlockReader`
    plus `LinkReader` and `LinkWriter`.
  - **`Memory` and `MemoryHandle` both satisfy these protocols.**
- **`LineageDAG` and `VersionedRecords` are composed, not new storage.**
  `LineageDAG` wraps `PropertyGraph`. `VersionedRecords` wraps
  `KeyedCollection` and `EventLog`.

## The path of one operation

### `memory.put(...)`

A create call through a trusted `Memory` object:

1. **Open the operation context.** `Memory` opens one `HookContext`. The
   context holds the principal that acts, the start time, and the attributes
   of the memory.
2. **Authorize the parent, if there is one.** `Memory` reads the parent block
   from the backend. Then it checks `WRITE` and `TRAVERSE_DOWN` on the parent.
   It reports a refusal to `on_denied` and raises it as `AccessDenied`.
3. **Encode the value.** `CodecRegistry.encode` changes the application value
   into storage data plus a content type.
4. **Build the block.** The block gets an id, a type, the encoded value, the
   parent id, metadata, a policy, and a creator. The policy is the explicit
   argument or `Memory.default_policy`.
5. **Call `before_put`.** Hooks can change the value, metadata, content type,
   and policy. A change to a different field, for example `parent_id`,
   `created_by`, or `version`, raises `ValueError`. So a hook cannot move the
   block into a different subtree after the parent check passes. A hook also
   cannot change the owner that `AccessRule` checks.
6. **Write once.** `backend.put_block` stores the block.
7. **Call `after_put` and decode.** `Memory` reports the stored block to
   `after_put`. Then it decodes the block and returns it with the application
   value.

### `handle.search(...)`

A search through a bounded handle shows how the 2 checks work together:

1. **The handle narrows the query.** It applies its own root, its default
   scope, and its capability limits before the query reaches `Memory`. The
   handle refuses a scope that it does not permit.
2. **`Memory` authorizes the query root.** It checks `SEARCH` on the root
   block. It also checks `TRAVERSE_DOWN`, `TRAVERSE_UP`, or `READ`. The scope
   decides which one.
3. **`before_search` can rewrite the query.** `Memory` normalizes and
   authorizes the rewritten query again. So a hook cannot widen the root or the
   scope. Through a handle, the rewritten query also goes through the handle
   checks from step 1 again. The handle refuses a scope that it does not
   permit.
4. **The backend returns one page** of results in the search order.
5. **`Memory` filters and decodes the page.** It filters the results against
   block policies and checks the query scope again. Then it decodes the
   results that it keeps. It gets the next page until it has `limit` visible
   results, or until the backend has no more results.
6. **`after_search` can change the list.** Then the same filter runs a second
   time over the list that the hooks returned. `Memory` keeps one verdict for
   each block id, so it reads a block once for both passes.

- **The limit counts visible results.** The filter runs before the count.
  So blocks that the caller cannot see do not change which visible results
  the caller gets.
- **A filtered read returns less and does not refuse.** The caller learns
  nothing about the blocks that it cannot see.
- **The right that each operation checks** is in
  [Detailed architecture](04-detailed-architecture.md#authorization-per-operation).

## Boundaries

| Boundary | Enforced by | What crosses it |
| --- | --- | --- |
| Public API | `genie.__init__` | Each exported name. No other import is supported |
| Access | `MemoryHandle` plus `MemoryPolicy` | Only blocks inside the handle root with a policy that permits the principal that acts |
| Storage | `MemoryBackend` and the capability protocols | Encoded `MemoryBlock` and `MemoryLink` values, `MemoryQuery`, `SearchResult` |
| Value | `MemoryCodec` | The application value goes in. Storage data plus a content type come out |
| Observation | `HookRegistry` | `HookContext` and typed event payloads, never raw backend objects |

- **The trust boundary is below the handle.** `Memory`, backends, the policy
  engine, the hook registry, and the codec registry belong in trusted
  application code. Agents get a rooted `MemoryHandle`, or a smaller tool that
  the application writes over a handle.
- **Raw backend access skips all of these layers.** A direct call to a backend
  bypasses handles, policies, hooks, and codecs. This is expected. It is the
  reason that the backend object stays in trusted code.
- **Child handles only narrow.** A child cannot change the principal, widen the
  root, or restore a capability that its parent disabled.
- **State for the whole backend stays above the handle.** A protected DAG
  relation changes link writes for all callers. So only
  `Memory.protect_relation` starts the protection, and `MemoryHandle` has no
  such method.

## Extension seams

| Extension point | Contract | Default | Typical reason to replace it |
| --- | --- | --- | --- |
| Storage | `MemoryBackend` | `InMemoryBackend` | Use a real database, or route blocks to several databases |
| Access rules | `PolicyEngine` and `MemoryPolicy` | `PolicyEngine`, `owner_private()` | Express a rule for ownership or shared access that the built-in rules do not cover |
| Observation | `MemoryHook` in a `HookRegistry` | Empty registry | Audit trails, metrics, validation, enrichment at `before_put` |
| Value translation | `MemoryCodec` in a `CodecRegistry` | `JsonCodec` | Store a value type that JSON cannot hold |
| Organization | Structure facades | None | Arrange blocks without a new storage model |

- **Each extension point is a protocol or a small class, not a plugin
  system.** There is no registry file, no entry point, and no discovery step.
  The application gives the object to the `Memory` constructor.
- **Optional backend capabilities are separate protocols.** `AcyclicLinkStore`,
  `KeyedStore`, `SequenceStore`, `TemporalStore`, `TemporalIndex`, and
  `RouteCatalog` are next to the core contract. So an adapter can ship without them, and a facade can
  check for the capability that it needs.
- **If a backend does not have a capability, the call raises
  `UnsupportedOperation`.** A shipped adapter raises the same error if its
  driver is not installed.

## How facades attach

- **Facades take an access object.** The access object is a `Memory` or a
  `MemoryHandle`. Each block read and write goes through it. So the scope and
  policy checks are the same as for a direct call.
- **Facades that keep records also take a store.** `KeyedCollection`,
  `EventLog`, `TemporalCollection`, and `VersionedRecords` need a backend
  capability for their bindings, entries, and sequences. `OwnershipTree`,
  `PropertyGraph`, and `LineageDAG` need no extra store, because parents and
  links hold their state.
- **A backend that routes blocks resolves to one route.** If the store argument
  is a `BlockRouter`, the facade binds to the backend that owns the block that
  anchors the structure. A container and its subtree always share one route.
- **Membership is not ownership.** Key bindings, log entries, temporal entries,
  and revisions refer to blocks by id. They never change a stored parent. So a
  subtree search reaches a member only if the application also created that
  member under the container.

## Cross-cutting concerns

- **Access control** is 2 independent checks: the handle scope and the block
  policy. The operation continues only if both checks permit it. Policies do
  not inherit through ownership. A policy is fixed at the creation of the
  block.
- **Observability** goes through hooks. Each operation that starts reports
  exactly one outcome event. Each policy refusal and each handle refusal
  reports its subject and the check that caused it. A filtered read of
  `Memory` or a handle is not a refusal, so hooks get no report about the
  items that it leaves out. A structure facade listing asks for each member,
  so it reports each hidden member.
- **Consistency** is optimistic. Block updates carry an expected version. A
  recursive delete sends the exact subtree snapshot that it observed. A
  concurrent change raises `MemoryConflict`, and the stale work does not
  commit.
- **Atomic behavior belongs to the backend.** Some operations cannot be a safe
  portable read followed by a write. Such an operation becomes a backend
  capability with contract and concurrency tests. It is not a best-effort
  sequence in a facade.
- **Genie errors are one family, except `AccessDenied`.** All other Genie
  error types derive from `GenieError`: `MemoryNotFound`, `MemoryConflict`,
  `UnsupportedOperation`, `HookRecursionError`, and the `BackendError` group.
  Adapters map storage failures to the `BackendError` group. `AccessDenied`
  derives from `PermissionError`.

## Left out on purpose

- **No process of its own:** no server, no worker, no background job, no
  retry loop, no cache layer.
- **No embeddings and no policy to rank results.** Search is a backend
  capability. Each backend decides how it scores a match.
- **No admin override and no policy mutation.** Authority stays where the
  creator put it.
- **No policy inheritance through ownership.** `write` on a block gives
  authority over the existence of the blocks that it owns. That is the only
  implied power.
- **No memory taxonomy.** Block types, value shapes, retrieval strategy, and
  agent behavior stay in the application.

## Related reading

- [Overview](01-overview.md) — the model and the reasons for it
- [Detailed architecture](04-detailed-architecture.md) — module internals
- [Repository organization](07-repo-organization.md) — the location of each component
- [Memory and handles](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md) — the details for each area
- [Queries](../lib/src/genie/query/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
