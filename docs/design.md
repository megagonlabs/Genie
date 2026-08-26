# Genie Design

Genie supplies storage-independent building blocks for application memory. It
defines addressable values, ownership, relationships, scoped access, and
composable structures. Applications define agents, schemas, memory meaning,
retrieval strategy, storage layout, and operational infrastructure.

## Architecture

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

`Memory` and one backend object are the required layers. Handles and structure
facades are optional views over the same blocks, and a facade accepts either a
`Memory` or a `MemoryHandle`. A single backend may coordinate several databases
or indexes behind the same contract.

Keep `Memory`, backends, policy engines, hooks, and codec registries in trusted
code. Give agents rooted handles or smaller application-defined tools.

## Model and safety

`MemoryBlock` is the addressable unit. It has a stable id, application-defined
type and value, optional parent, metadata, content type, policy, creator,
timestamps, and an update version.

- Parent/child relationships mean ownership. They define subtree scope,
  traversal, and recursive deletion.
- `MemoryLink` values are directed, typed, and non-owning. Updating a link
  merges metadata while preserving identity, topology, relation, and creation
  time.
- Mounts point to another root but are not followed by ordinary subtree search,
  traversal, or deletion.
- Codecs translate application values at the storage boundary.

Every public memory operation acts as a `Principal`. Access requires both an
allowed handle scope and an allowed block policy. Child handles may narrow
access but cannot change principal or restore disabled capabilities. New blocks
are owner-private by default; policies do not inherit through ownership. A
policy is chosen when a block is created, from an explicit argument,
`Memory.default_policy`, or a `before_put` hook, and stays stable across later
updates. Delegating across principals therefore needs an explicit grant in each
direction, and reads that a policy filters return fewer results rather than
refusing.

Block updates use optimistic versions. Recursive deletion verifies the exact
authorized subtree snapshot. Concurrent changes raise `MemoryConflict` rather
than committing stale work.

Hooks cover block lifecycle, search, link mutations, and explicit key, log, and
temporal changes. Every operation that starts also reports one terminal outcome
event, and every policy or handle refusal is reported with its subject and the
check that produced it, so denials and failures stay observable. Every event
carries its start time and inherited string attributes, and caller-defined
scopes group the operations of one agent run, turn, or tool call. Changed
searches are authorized again and final results are filtered again. That
filtering runs after a backend has already applied the query limit, so a result
page can be shorter than the limit the caller asked for. Reentrant callbacks are
skipped with bounded causal context. Callbacks that observe already-committed
work are isolated from one another, so one failing sink cannot cost the others
the record they are owed, and a sink that declares itself fail-open cannot fail
the caller at all. Hooks run synchronously; external side effects are not
backend transactions.

## Optional structures

Facades organize references to existing blocks without changing application
values. Any subset can be used, and one block can participate in all seven.

| Facade | Structure |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy |
| `PropertyGraph` | Typed graph over links |
| `KeyedCollection` | Namespaced key map |
| `EventLog` | Ordered append log |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Property graph with cycle prevention |
| `VersionedRecords` | Keyed identity with ordered revisions |

Facades use `Memory` or `MemoryHandle`, preserving normal scope and policy
checks. `LineageDAG` reuses `PropertyGraph`; `VersionedRecords` reuses keyed
identity and an event log. Atomic keys, sequences, temporal membership, and
cycle-protected insertion are optional backend capabilities. A lineage cycle
check and edge insertion must be one atomic operation.

Facade membership is not ownership. Key bindings, log entries, temporal
entries, and revisions reference blocks by id without changing stored parents,
so subtree search and traversal reach a structure's members only when the
application also creates them under the container block.

## Backends

`MemoryBackend` structurally combines block storage, ownership traversal,
links, and search. `Memory` receives one backend object, which may coordinate
multiple databases or indexes.

| Implementation | Role |
| --- | --- |
| `InMemoryBackend` | Process-local reference backend; all optional capabilities |
| `SQLiteBackend` | Persistent local backend using built-in `sqlite3`; all optional capabilities |
| `Neo4jBackend` | Optional official-driver graph backend; all optional capabilities |
| `RoutedBackend` | Composite backend with application-defined storage routes |

Block, link, and structural stores hold the source of truth. Ownership
traversal and search are read-only views over that state, and search depends on
stored links as well as stored blocks because link scope is resolved from links.
`Memory` resolves ownership only through the traversal interface, so a backend
that supplies a separate traversal index defines subtree scope, traversal, and
recursive deletion there and must keep it consistent with stored parents.

Backends must preserve ownership, clean dependent structural state, enforce
conditional writes atomically, and map storage failures to Genie errors. Raw
backend access bypasses handles and policies. Applications remain responsible
for deployment, backup, monitoring, and performance requirements.

## Detailed guides

[Memory](../genie/memory/README.md) covers blocks and handles;
[structures](../genie/structures/README.md) covers composition; and
[Backends](../genie/backend/README.md) defines adapter contracts. See also
[Queries](../genie/query/README.md), [Policies](../genie/policy/README.md),
[Hooks](../genie/hooks/README.md), and [Codecs](../genie/codecs/README.md).
Runnable patterns live in [Examples](../examples/README.md) and
[Lamps](../lamps/README.md).
