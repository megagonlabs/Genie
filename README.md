# Genie

Genie gives developers typed building blocks for making memory systems. It
provides memory blocks, parent/child ownership, typed relationships, scoped
access, and optional structure views. Genie does not depend on any agent
framework, database, memory taxonomy, retrieval strategy, or stored value type.

> Genie is alpha software. Public APIs may still change.

## What it provides

- `MemoryBlock` values. Your application chooses their type, metadata, policy,
  and version.
- Parent/child ownership, which controls containment, subtree scope, and
  lifecycle.
- `MemoryLink` relationships, which do not imply ownership.
- `MemoryHandle` objects that give agents and services only the access they
  need.
- Structure facades for trees, graphs, keys, logs, time, lineage, and versions.
- Storage protocols, with in-memory, SQLite, and optional Neo4j
  implementations, plus a routed backend that combines several of them.
- Separate policies, hooks, queries, and codecs.

Genie does not run agents, plan their work, create embeddings, host a database,
or define one memory schema for everyone.

## Install

Genie needs Python 3.11 or newer. It has no required runtime dependencies. From
a cloned repository:

```bash
conda run -n "genie" python -m pip install -e .
```

## Quick start

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
workspace = memory.put(type="project", value={"name": "Write proposal"})
requirement = memory.put(
    parent=workspace,
    type="requirement",
    value="Keep the proposal concise.",
)

agent_memory = memory.get_handle(root=workspace, principal="agent:planner")
agent_memory.put(type="observation", value="Use short sections.")

results = agent_memory.search("concise")
assert results[0].block.id == requirement.id
```

By default, only the creator of a block can read it. Keep the full `Memory`
object and the backend inside trusted application code. Give agents rooted
handles, or tools that you write yourself.

## Storage

`InMemoryBackend` is the reference backend. It needs no extra packages, so it
suits tests, examples, and prototypes. `SQLiteBackend` adds local persistent
storage. The optional `Neo4jBackend` adds graph storage through the official
Neo4j driver. Both persistent backends support all seven structure facades.
SQLite uses the driver that ships with Python:

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

You do not need the SQLite command-line program. The [backend
guide](genie/backend/README.md) explains the storage contracts, how each
backend works, and what to think about in production. Install `genie[neo4j]` if
you use Neo4j.

## Model and structures

```text
Memory
  project
    requirement
    observation

MemoryLink
  requirement --supports--> observation
```

Ownership decides scope and lifecycle. Links describe relationships without
ownership. Your application decides what block types, values, metadata, and
links mean.

| Facade | Purpose |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchies |
| `PropertyGraph` | Typed relationships and traversal |
| `KeyedCollection` | Namespaced key lookup |
| `EventLog` | Stable append order |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Derivation history, protected from cycles |
| `VersionedRecords` | Stable identities and ordered revisions |

All facades point at the same blocks. You can combine them freely, and they
keep the normal handle scope and policy checks.

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

Only two layers are required: `Memory` and one backend object. Handles and
structure facades are optional views over the same blocks, and a facade accepts
either a `Memory` or a `MemoryHandle`. One backend may run several databases or
indexes behind the same contract.

Keep `Memory`, backends, policy engines, hooks, and codec registries in trusted
code. Give agents rooted handles, or smaller tools that you define.

## Learn more

- [Architecture](docs/design.md)
- [Memory and handles](genie/memory/README.md)
- [Structures](genie/structures/README.md)
- [Backends](genie/backend/README.md)
- [Queries](genie/query/README.md)
- [Policies](genie/policy/README.md)
- [Hooks](genie/hooks/README.md)
- [Codecs](genie/codecs/README.md)
- [Examples](examples/README.md) and [Lamps](lamps/README.md)

## Develop

```bash
conda run -n "genie" python -m pytest
conda run -n "genie" python -m compileall -q genie tests examples lamps
```

Run `./scripts/test_full.sh` to run the whole suite against a throwaway Neo4j
container. Any extra arguments are passed on to pytest.
