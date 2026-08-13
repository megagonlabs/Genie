# Genie

Genie provides typed building blocks for developers creating memory systems:
memory blocks, parent/child ownership, typed relationships, scoped access, and
composable structural views. It stays independent of agent frameworks,
databases, memory taxonomies, retrieval strategies, and stored value types.

> Genie is alpha software. Public APIs may evolve.

## What it provides

- `MemoryBlock` values with application-defined types, metadata, policies, and
  versions.
- Parent/child ownership for containment, subtree scope, and lifecycle.
- `MemoryLink` relationships that do not imply ownership.
- Least-privilege `MemoryHandle` objects for agents and services.
- Structure facades for trees, graphs, keys, logs, time, lineage, and versions.
- Storage protocols plus in-memory and SQLite implementations.
- Separate policies, hooks, queries, and codecs.

Genie does not provide agent execution, planning, embeddings, a managed
database service, or a universal memory schema.

## Install

Genie requires Python 3.11 or newer and has no required runtime dependencies.
From a cloned repository:

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

New blocks are owner-private by default. Keep the full `Memory` and backend in
trusted application code; give agents rooted handles or application-defined
tools.

## Storage

`InMemoryBackend` is the dependency-free reference backend for tests, examples,
and prototypes. `SQLiteBackend` adds embedded persistence, while the optional
`Neo4jBackend` adds graph persistence through the official Neo4j driver. Both
support all seven structure facades. SQLite uses Python's built-in driver:

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The command-line SQLite program is not required. See the [backend
guide](genie/backend/README.md) for storage contracts, implementation details,
and production considerations. Install `genie[neo4j]` when using Neo4j.

## Model and structures

```text
Memory
  project
    requirement
    observation

MemoryLink
  requirement --supports--> observation
```

Ownership defines scope and lifecycle. Links express non-owning relationships.
Applications define the meaning of block types, values, metadata, and links.

| Facade | Purpose |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchies |
| `PropertyGraph` | Typed relationships and traversal |
| `KeyedCollection` | Namespaced key lookup |
| `EventLog` | Stable append order |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Cycle-protected derivation history |
| `VersionedRecords` | Stable identities and ordered revisions |

Facades reference the same blocks, can be combined freely, and preserve normal
handle scope and policy checks.

## Architecture

```text
Application or agent tools
          |
    MemoryHandle        restricted scope and principal
          |
       Memory           policy, hooks, codecs, operation flow
          |
   MemoryBackend        blocks, traversal, links, search
          |
Application storage     SQL, document, graph, vector, or hybrid
```

## Learn more

- [Architecture](docs/design.md)
- [Memory and handles](genie/memory/README.md)
- [Structures](genie/structures/README.md)
- [Backends](genie/backend/README.md)
- [Queries](genie/query/README.md)
- [Policies](genie/policy/README.md)
- [Hooks](genie/hooks/README.md)
- [Codecs](genie/codecs/README.md)
- [Examples](examples/README.md) and [lamps](lamps/README.md)

## Develop

```bash
conda run -n "genie" python -m pytest
conda run -n "genie" python -m compileall -q genie tests examples lamps
```

Run `./scripts/test_full.sh` for the complete suite against a disposable Neo4j
container. Extra arguments are passed to pytest.
