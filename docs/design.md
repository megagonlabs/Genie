# Genie Design

Genie is a small library for developers who build memory systems. It provides
memory blocks, ownership, relationships, scoped access, and extension
interfaces. Applications decide what memories mean, how agents use them, and
which databases or retrieval methods to use.

## Goals

Genie is designed to be:

- independent of agent frameworks;
- independent of databases and search engines;
- independent of memory taxonomies and stored value types;
- safe to expose through restricted handles;
- easy to extend with backends, policies, hooks, and codecs; and
- small enough to use as a library rather than a memory product.

Genie does not manage agents, planning, embeddings, application workflows, or
one universal memory schema. Those choices remain in application code.

## Architecture

```text
Application or agent tools
          |
    MemoryHandle               restricted scope and principal
          |
       Memory                  policy, hooks, codecs, operation flow
          |
   MemoryBackend               blocks, traversal, links, search
          |
Database or composed storage   chosen by the backend implementation
```

`Memory` is the trusted application interface. `MemoryHandle` gives a caller a
smaller view of the same memory. Both use a backend for persistence and
retrieval.

Applications should keep `Memory`, backends, hooks, and codec registries in
trusted code. Agents should usually receive a rooted handle or a smaller set of
application-defined tools.

## Core model

`MemoryBlock` is the basic unit. It has a stable id, application-defined type
and value, metadata, optional parent, policy, and lifecycle information.

Two relationships organize blocks:

- A parent/child relationship means ownership. It controls subtree scope,
  traversal, and recursive deletion.
- A `MemoryLink` connects blocks without ownership. Links represent references,
  provenance, graph edges, and mounts.

Codecs translate application values to storage-safe values. Policies control
operations for a principal. Hooks observe or customize the public operation
flow.

## Optional structures

Structure facades organize existing blocks without changing their values:

| Facade | Main abstraction |
| --- | --- |
| `OwnershipTree` | Rooted hierarchy |
| `PropertyGraph` | Typed graph |
| `KeyedCollection` | Namespaced key map |
| `EventLog` | Ordered append-only references |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Cycle-protected derivation graph |
| `VersionedRecords` | Stable identities and ordered revisions |

Facades are optional and composable. One block can belong to several
structures. A compound memory can expose one facade, a subset, or all seven.

## Backend boundary

The public API receives one `MemoryBackend`. The adapter implements block
storage, ownership traversal, links, and search. Internally, it may use one
database or coordinate several systems.

Optional structures request smaller capabilities for atomic keys, sequences,
temporal membership, and acyclic graph insertion. A backend implements only
the capabilities it supports.

`InMemoryBackend` is the reference implementation for tests and examples.
Production systems should provide an adapter with appropriate persistence,
transactions, observability, and backup behavior.

## Access model

Every operation uses an acting `Principal`. A block policy decides whether the
principal may read, write, search, or traverse that block.

A handle adds a separate scope boundary. A request succeeds only when the
target is inside the allowed scope and the policy permits the operation. Child
handles may reduce access but cannot increase it.

New blocks are owner-private by default. Policy inheritance from parents is not
automatic.

## Important invariants

- Parent/child ownership has no cycles.
- Recursive deletion follows ownership, not links or mounts.
- Link metadata may change, but link identity and topology stay stable.
- Bounded handles cannot read or write outside their root.
- Search results are filtered by scope and policy.
- Structure facades resolve blocks through normal memory access.
- Atomic structure operations are the backend's responsibility.
- Application values and business meaning stay outside the core library.

## Package guides

Detailed design notes live beside the code they describe:

- [`genie.memory`](../genie/memory/README.md): blocks, ownership, links,
  `Memory`, handles, mounts, and safety rules.
- [`genie.structures`](../genie/structures/README.md): the seven facades,
  structural records, composition, and capability requirements.
- [`genie.backend`](../genie/backend/README.md): backend protocols, store and
  index roles, routing, and adapter contracts.
- [`genie.query`](../genie/query/README.md): search scopes, filters, and query
  authorization flow.
- [`genie.policy`](../genie/policy/README.md): principals, access rules, custom
  operations, and security guidance.
- [`genie.hooks`](../genie/hooks/README.md): hook points, ordering,
  authorization boundaries, and failure behavior.
- [`genie.codecs`](../genie/codecs/README.md): value encoding, registry
  selection, and custom codecs.

Runnable examples are organized under [`examples/`](../examples/README.md).
The `lamps/` directory contains larger examples inspired by existing memory
systems while keeping their business logic outside Genie core.
