# Genie Overview

Genie is a Python library of typed building blocks for developers who build
their own memory systems.

## Objective

- **The problem:** Each team that gives long-lived memory to an agent or a service builds the same base parts again. These parts are addressable records, containment, relationships, access limits for each caller, and a store for the records.
- **The goal:** Genie supplies these base parts as a small, typed core that is independent of the storage. An application then writes only the parts that are specific to it.
- **The audience:** Developers who want to control their own memory design. Genie gives them the mechanism. All decisions about the meaning of values stay with them.
- **The boundary:** Genie stores and protects values. It never decides what a value means or what an agent does with it.
- **Out of scope:** Genie does not run agents or plan their work. It does not create embeddings or host a database. It does not define one memory taxonomy for all applications.
- **Status:** This document describes current behavior only.

## What It Does

### A worked example

A support service keeps one block for each case. The case owns all the
information that the service learns during its work on the case. A triage agent
gets access to that one case only.

```python
from genie import InMemoryBackend, Memory, MemoryPolicy


memory = Memory(backend=InMemoryBackend(), principal="service:support")
shared = MemoryPolicy.owner_or(principals={"agent:triage"})

case = memory.put(
    type="case",
    value={"customer": "Northwind", "subject": "CSV export stops early"},
    policy=shared,
)
report = memory.put(
    parent=case,
    type="message",
    value="Our weekly CSV export stops at 500 rows.",
    policy=shared,
)
finding = memory.put(
    parent=case,
    type="finding",
    value="The export returns only the first page of results.",
    policy=shared,
)
memory.add_link(finding, report, "cites")

triage = memory.get_handle(root=case, principal="agent:triage")
triage.children()                # [message, finding]
triage.search("export")          # case, message, finding
```

- **`case` owns `report` and `finding`,** so the case defines their scope and their lifecycle. A delete of the case also deletes them.
- **`cites` is a link, not ownership.** The finding refers to the message that it came from. Neither block contains the other.
- **`triage` cannot leave the case.** A search through that handle never reaches the case of a different customer. This is true although all cases are in one `Memory`.
- **The explicit `shared` policy gives the agent access.** Without `shared`, the blocks get the default `owner_private()` policy, and only `service:support` can read them. A principal that `shared` does not list gets `AccessDenied`.
- **This code does not tell Genie what a `case` means.** The application chooses the `type` strings, the value shapes, and the `cites` relation.

### Core model

- **`MemoryBlock` is the addressable unit.** It has a stable id, an application-defined `type` and `value`, an optional parent, and metadata. It also has a content type, a `MemoryPolicy`, a creator, timestamps, and an update version.
- **Parent/child means ownership** and nothing else. It defines subtree scope, traversal, and recursive delete.
- **`MemoryLink` means relationship.** Links are directed and typed. They do not mean ownership. An update of a link merges the metadata. The update keeps the identity, topology, relation, and creation time of the link.
- **Mounts refer to a different root.** Ordinary subtree search, traversal, and delete do not follow them.
- **`MemoryCodec` values translate application values at the storage boundary.** So a backend stores data that it understands, and the application does not convert its own types manually.

### Scoped access

- **Each public operation acts as a `Principal`.** A principal is a user, an agent, or a service. Access needs 2 permissions: the handle scope permits the operation, and the block `MemoryPolicy` permits it too.
- **`MemoryPolicy.owner_private()` is the default** for new blocks. With this policy, only the creator can `read`, `write`, `search`, `traverse_up`, and `traverse_down` the block.
- **`MemoryHandle` limits what a caller can reach.** A handle has a root block, a principal, and a set of `HandleCapability` values. Child handles only narrow. They cannot change the principal or restore a disabled capability.
- **Policies do not inherit** through ownership. But `write` on a block gives authority over the existence of all blocks that it owns. So `write` on the target block alone authorizes a recursive delete.
- **Filtered reads return fewer results** and do not refuse. So a caller learns nothing about blocks that it cannot see.
- **Trust boundary:** `Memory`, backends, policy engines, hooks, and codec registries stay in trusted code. Agents get rooted handles, or smaller tools that the application defines.

### Optional structures

Facades organize references to blocks. They do not change the meaning of the
values. Any subset of facades works over the same blocks. One block can be part
of all 7 facades.

| Facade | Structure |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy |
| `PropertyGraph` | Typed graph over links |
| `KeyedCollection` | Namespaced key map |
| `EventLog` | Ordered append log |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Property graph with cycle prevention |
| `VersionedRecords` | Keyed identity with ordered revisions |

- **A facade takes a `Memory` or a `MemoryHandle`.** It keeps the normal scope and policy checks.
- **Membership is not ownership.** Key bindings, log entries, temporal entries, and revisions refer to blocks by id. They do not change the stored parents.

### Storage

- **`MemoryBackend` combines block storage, ownership traversal, links, and search.** `Memory` receives one backend object. That object can coordinate several databases or indexes behind the contract.
- **`InMemoryBackend` is the reference backend.** `SQLiteBackend` adds local persistence through the driver in the Python standard library. The 2 backends do not need an extra package.
- **`PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` are optional.** Each one needs its own extra. `RoutedBackend` composes several backends behind application-defined routes.
- **Each shipped adapter supports all 7 facades.** It guards its third-party imports. It raises `UnsupportedOperation` if its driver is not installed.
- **Search is a backend capability** (`SearchIndex`) with portable query and result types in `genie.query`. It is not a separate integration point.
- **Applications own deployment,** backup, monitoring, and performance requirements.

### Extension points

- **Policies** decide who can act on a block.
- **Hooks** observe the block lifecycle, search, link mutations, and structural changes. Each operation that starts reports exactly one outcome event. Each refusal reports its subject and the check that caused it.
- **Queries** give a portable description of a search. So the same `MemoryQuery` works on different backends.
- **Codecs** translate values at the storage boundary.

### Layers

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

- **Required:** `Memory` and one backend object.
- **Optional:** handles and structure facades. They are views over the same blocks.

## Design Philosophy

- **Mechanism, not meaning.** Genie owns block addresses, containment, relationships, access checks, and the storage contract. Block types, value shapes, memory taxonomy, retrieval strategy, and agent behavior stay in the application.
- **Independent of 4 things:** the agent framework, the database, the memory use case, and the stored value type. A public API that couples Genie to one of them does not belong in the core.
- **Database-agnostic, not database-free.** Real adapters ship with Genie, so the contracts stay correct for real databases. The adapters stay optional and behind `MemoryBackend`.
- **No third-party runtime dependencies in the core.** An install of Genie adds no other package. A driver comes only with the extra that needs it.
- **Ownership and relationships are separate ideas.** Parent/child is for containment and lifecycle. Links and mounts are for all other relationships. Because the 2 ideas stay separate, subtree scope has one predictable meaning.
- **Least privilege by default.** A new block is private to its creator. A handle only narrows. Access needs the agreement of both the handle scope and the block policy.
- **A policy is decided once.** At creation time, it comes from an explicit argument, `Memory.default_policy`, or a `before_put` hook. Then it stays the same for the life of the block. There is no policy mutation path and no admin override. So authority stays where the creator put it.
- **Narrow capability protocols over concrete classes.** Facades depend on the small protocol that they need, not on a backend class. No code assumes that each backend implements all optional capabilities.
- **Stores own the truth, and indexes are derived.** Block, link, and structural stores hold state. Ownership traversal and search are read-only views over that state. If an adapter keeps its own traversal index, the adapter keeps that index consistent with the stored parents.
- **Composition, not duplication.** `LineageDAG` reuses `PropertyGraph`. `VersionedRecords` reuses keyed identity and an event log. A new structure reuses the facades over the same blocks and does not add a second storage model.
- **Stale work is refused, not committed.** Block updates use optimistic versions. A recursive delete verifies the exact subtree snapshot that it observed. A block that is deleted and created again with the same id is a different block, also at the same version. A concurrent change raises `MemoryConflict`.
- **Atomic behavior belongs in the backend contract.** Some operations cannot be a safe portable read followed by a write. Such an operation becomes an atomic backend capability with contract and concurrency tests. It is not a best-effort sequence in a facade.
- **Denials stay observable.** Hooks report each policy refusal and each handle refusal with its subject and check. So a filtered result is auditable and not silent.
- **Explicit over hidden.** Genie has no hidden global state. Tests and examples have no non-deterministic behavior. Public APIs are typed. Exceptions are predictable where they affect users.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components, boundaries, and extension points
- [Detailed architecture](04-detailed-architecture.md) — module internals
- [Memory and handles](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md)
- [Queries](../lib/src/genie/query/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — runnable patterns
- [CONTRIBUTING.md](../CONTRIBUTING.md) — the boundaries for a change
