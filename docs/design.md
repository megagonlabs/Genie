# Genie Design

Storage-independent building blocks for application memory: addressable values,
ownership, relationships, scoped access, and composable structures.
Applications define agents, schemas, memory meaning, retrieval strategy, storage
layout, and operational infrastructure.

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

- **Required layers:** `Memory` and one backend object.
- **Optional layers:** handles and structure facades. They are views over the same blocks, and a facade accepts either a `Memory` or a `MemoryHandle`.
- **One backend object** may coordinate several databases or indexes behind the same contract.
- **Trusted code** holds `Memory`, backends, policy engines, hooks, and codec registries. Agents get rooted handles or smaller application-defined tools.

## Model

**`MemoryBlock`** is the addressable unit: stable id, application-defined type
and value, optional parent, metadata, content type, policy, creator,
timestamps, and an update version.

- **Parent/child means ownership.** It defines subtree scope, traversal, and recursive deletion.
- **`MemoryLink` values** are directed, typed, and non-owning. Updating a link merges metadata while preserving identity, topology, relation, and creation time.
- **Mounts** point to another root. Ordinary subtree search, traversal, and deletion do not follow them.
- **Codecs** translate application values at the storage boundary.

## Safety

- **Every public operation acts as a `Principal`.** Access needs both an allowed handle scope and an allowed block `MemoryPolicy`.
- **Child handles narrow only.** They cannot change principal or restore disabled capabilities.
- **`MemoryPolicy.owner_private()` is the default** for new blocks, so only the creator may `read`, `write`, `search`, `traverse_up`, and `traverse_down` them.
- **Policies do not inherit** through ownership. But `write` on a block is authority over the existence of everything it owns.
- **A policy is chosen at creation** — from an explicit argument, `Memory.default_policy`, or a `before_put` hook — and stays stable across later updates.
- **Delegating across principals** therefore needs an explicit grant in each direction.
- **Filtered reads return fewer results** rather than refusing.
- **Block updates use optimistic versions.** Recursive deletion is authorized by `write` on the target block alone, and it verifies the exact subtree snapshot it observed.
- **Concurrent changes raise `MemoryConflict`** rather than committing stale work.

## Hooks

- **Coverage:** block lifecycle, search, link mutations, and explicit key, log, and temporal changes.
- **Terminal event:** every operation that starts reports exactly one outcome event.
- **Refusals:** every policy or handle refusal is reported with its subject and the check that produced it, so denials and failures stay observable.
- **Context:** every event carries its start time and inherited string attributes. Caller-defined scopes group the operations of one agent run, turn, or tool call.
- **Re-authorization:** changed searches are authorized again and final results are filtered again. That filtering runs after a backend has already applied the query `limit`, so a result page can be shorter than the `limit` the caller asked for.
- **Recursion:** reentrant callbacks are skipped, with bounded causal context.
- **Isolation:** callbacks that observe already-committed work are isolated from one another, so one failing sink cannot cost the others the record they are owed. A sink that declares itself fail-open cannot fail the caller at all.
- **Timing:** hooks run synchronously, and their external side effects are not backend transactions.

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

- **Access object:** a facade uses `Memory` or `MemoryHandle`, and preserves normal scope and policy checks.
- **Reuse:** `LineageDAG` reuses `PropertyGraph`; `VersionedRecords` reuses keyed identity and an event log.
- **Optional backend capabilities:** atomic keys, sequences, temporal membership, and cycle-protected insertion. A lineage cycle check and edge insertion must be one atomic operation.
- **Membership is not ownership.** Key bindings, log entries, temporal entries, and revisions reference blocks by id without changing stored parents. So subtree search and traversal reach a structure's members only when the application also creates them under the container block.

## Backends

`MemoryBackend` structurally combines block storage, ownership traversal, links,
and search. `Memory` receives one backend object, which may coordinate multiple
databases or indexes.

| Implementation | Role |
| --- | --- |
| `InMemoryBackend` | Process-local reference backend; all optional capabilities |
| `SQLiteBackend` | Persistent local backend using built-in `sqlite3`; all optional capabilities |
| `PostgresBackend` | Optional official-driver PostgreSQL backend; all optional capabilities |
| `Neo4jBackend` | Optional official-driver graph backend; all optional capabilities |
| `MongoDBBackend` | Optional official-driver document backend; all optional capabilities |
| `RedisBackend` | Optional official-client key-value backend; all optional capabilities |
| `RoutedBackend` | Composite backend with application-defined storage routes |

- **Stores own the truth:** block, link, and structural stores.
- **Indexes are derived:** ownership traversal and search are read-only views over that state. Search depends on stored links as well as stored blocks, because link scope is resolved from links.
- **Traversal defines scope.** `Memory` resolves ownership only through the traversal interface, so a backend that supplies a separate traversal index defines subtree scope, traversal, and recursive deletion there, and must keep it consistent with stored parents.
- **Adapter duties:** preserve ownership, clean dependent structural state, enforce conditional writes atomically, and map storage failures to Genie errors.
- **Raw backend access bypasses handles and policies.**
- **Applications own deployment,** backup, monitoring, and performance requirements.

## Detailed guides

- [Memory](../genie/memory/README.md) — blocks and handles
- [Structures](../genie/structures/README.md) — composition
- [Backends](../genie/backend/README.md) — adapter contracts
- [Queries](../genie/query/README.md), [Policies](../genie/policy/README.md), [Hooks](../genie/hooks/README.md), [Codecs](../genie/codecs/README.md)
- [Examples](../examples/README.md) and [Lamps](../lamps/README.md) — runnable patterns
