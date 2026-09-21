# Genie Overview

Genie is a Python library of typed building blocks for developers who build
their own memory systems.

## Objective

- **The problem:** every team that gives an agent or a service long-lived memory rebuilds the same plumbing — addressable records, containment, relationships, per-caller access limits, and a store to hold them.
- **The goal:** ship that plumbing as a small, typed, storage-independent core, so an application only writes the parts that are specific to it.
- **The audience:** developers who want to own their memory design. Genie gives them the mechanism and leaves every decision about meaning to them.
- **The boundary:** Genie stores and protects values. It never decides what a value means, or what an agent should do with it.
- **Out of scope:** running agents, planning their work, creating embeddings, hosting a database, and defining one memory taxonomy for everyone.
- **Status:** alpha. Public APIs may still change, and this document describes current behavior only.

## What It Does

### A worked example

A support service keeps one block per case. The case owns everything learned
while working on it, and a triage agent gets access to that one case only.

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

- **`case` owns `report` and `finding`,** so the case defines their scope and their lifecycle. Deleting the case deletes them.
- **`cites` is a link, not ownership.** The finding points at the message it came from, and neither block contains the other.
- **`triage` cannot leave the case.** Searching through that handle never reaches another customer's case, even though all cases live in one `Memory`.
- **The explicit `shared` policy is what lets the agent in.** Without it, the blocks would keep the default `owner_private()` and only `service:support` could read them. A principal that `shared` does not list gets `AccessDenied`.
- **Nothing here tells Genie what a `case` means.** The `type` strings, the value shapes, and the `cites` relation are all the application's choice.

### Core model

- **`MemoryBlock` is the addressable unit** — a stable id, an application-defined `type` and `value`, an optional parent, metadata, a content type, a `MemoryPolicy`, a creator, timestamps, and an update version.
- **Parent/child means ownership,** and nothing else. It defines subtree scope, traversal, and recursive deletion.
- **`MemoryLink` means relationship.** Links are directed and typed, and they carry no ownership. Updating a link merges metadata and keeps its identity, topology, relation, and creation time.
- **Mounts point at another root.** Ordinary subtree search, traversal, and deletion do not follow them.
- **`MemoryCodec` values translate application values at the storage boundary,** so a backend stores something it understands without the application flattening its own types by hand.

### Scoped access

- **Every public operation acts as a `Principal`** — a user, an agent, or a service. Access needs both an allowed handle scope and an allowed block `MemoryPolicy`.
- **`MemoryPolicy.owner_private()` is the default** for new blocks, so only the creator may `read`, `write`, `search`, `traverse_up`, and `traverse_down` them.
- **`MemoryHandle` bounds what a caller can reach** — a root block, a principal, and a set of `HandleCapability` values. Child handles narrow only. They cannot change principal or restore a disabled capability.
- **Policies do not inherit** through ownership. But `write` on a block is authority over the existence of everything it owns, so recursive deletion is authorized by `write` on the target block alone.
- **Filtered reads return fewer results** rather than refusing, so a caller learns nothing about blocks it cannot see.
- **Trust boundary:** keep `Memory`, backends, policy engines, hooks, and codec registries in trusted code. Give agents rooted handles, or smaller tools the application defines.

### Optional structures

Facades organize references to existing blocks without changing what the values
mean. Any subset works over the same blocks, and one block can take part in all
seven.

| Facade | Structure |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchy |
| `PropertyGraph` | Typed graph over links |
| `KeyedCollection` | Namespaced key map |
| `EventLog` | Ordered append log |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Property graph with cycle prevention |
| `VersionedRecords` | Keyed identity with ordered revisions |

- **A facade takes a `Memory` or a `MemoryHandle`,** and keeps the normal scope and policy checks.
- **Membership is not ownership.** Key bindings, log entries, temporal entries, and revisions reference blocks by id without changing stored parents.

### Storage

- **`MemoryBackend` combines block storage, ownership traversal, links, and search.** `Memory` receives one backend object, which may coordinate several databases or indexes behind that contract.
- **`InMemoryBackend` is the reference backend,** and `SQLiteBackend` adds local persistence through the driver that ships with Python. Neither needs an extra package.
- **`PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` are optional,** installed through the matching extra. `RoutedBackend` composes several backends behind application-defined routes.
- **Every shipped adapter supports all seven facades,** guards its third-party imports, and raises `UnsupportedOperation` when its driver is missing.
- **Search is a backend capability** (`SearchIndex`) with portable query and result types in `genie.query`. It is not a separate integration point.
- **Applications own deployment,** backup, monitoring, and performance requirements.

### Extension points

- **Policies** decide who may act on a block.
- **Hooks** observe block lifecycle, search, link mutations, and structural changes. Every operation that starts reports exactly one outcome event, and every refusal is reported with its subject and the check that produced it.
- **Queries** describe search portably, so the same `MemoryQuery` works across backends.
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
- **Optional:** handles and structure facades, which are views over the same blocks.

## Design Philosophy

- **Mechanism, not meaning.** Genie owns addressing, containment, relationships, access checks, and the storage contract. Block types, value shapes, memory taxonomy, retrieval strategy, and agent behavior stay in the application.
- **Independent of four things:** the agent framework, the database, the memory use case, and the stored value type. A public API that couples Genie to one of them does not belong in the core.
- **Database-agnostic, not database-free.** Real adapters ship so the contracts stay honest, but they stay optional and behind `MemoryBackend`.
- **No third-party runtime dependencies in the core.** Installing Genie pulls nothing in. Drivers arrive only with the extra that needs them.
- **Ownership and relationships are separate ideas.** Parent/child is for containment and lifecycle. Links and mounts are for everything else. Keeping them apart is what makes subtree scope mean one predictable thing.
- **Least privilege by default.** A new block is private to its creator, a handle only narrows, and access needs both handle scope and block policy to agree.
- **A policy is decided once.** It comes from an explicit argument, `Memory.default_policy`, or a `before_put` hook at creation time, and then stays stable for the life of the block. There is no policy mutation path and no admin override, so authority stays where the creator put it.
- **Narrow capability protocols over concrete classes.** Facades depend on the small protocol they need, not on a backend class, and no code assumes every backend implements every optional capability.
- **Stores own the truth; indexes are derived.** Block, link, and structural stores hold state. Ownership traversal and search are read-only views over it, and an adapter that keeps its own traversal index must keep it consistent with stored parents.
- **Compose instead of duplicating.** `LineageDAG` reuses `PropertyGraph`, and `VersionedRecords` reuses keyed identity and an event log. A new structure should reuse the existing facades over the same blocks rather than add a second storage model.
- **Refuse stale work instead of committing it.** Block updates use optimistic versions, recursive deletion verifies the exact subtree snapshot it observed, and a concurrent change raises `MemoryConflict`.
- **Atomic behavior belongs in the backend contract.** When an operation cannot be a safe portable read followed by a write, it becomes an atomic backend capability with contract and concurrency tests, not a best-effort sequence in a facade.
- **Denials stay observable.** Policy and handle refusals are reported through hooks with their subject and check, so a filtered result is auditable rather than silent.
- **Explicit over hidden.** No hidden global state, no non-deterministic behavior in tests or examples, typed public APIs, and predictable exceptions where they affect users.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components, boundaries, and extension seams
- [Detailed architecture](04-detailed-architecture.md) — module internals
- [Memory and handles](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md)
- [Queries](../lib/src/genie/query/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — runnable patterns
- [CONTRIBUTING.md](../CONTRIBUTING.md) — the boundaries a change should respect
