# Genie

Genie provides small, typed building blocks for developers creating memory
systems. It defines memory blocks, parent/child ownership, relationships, and
scoped access without requiring a specific agent framework, database, memory
schema, or retrieval strategy.

Use Genie to build your own memory system. Genie is not an agent framework or
an opinionated long-term memory product.

> Genie is currently alpha software. Public APIs may evolve.

## What Genie provides

- `MemoryBlock`: an independently addressable unit with an application-defined
  type and value.
- Parent/child ownership for containment, subtree access, and recursive
  operations.
- `MemoryLink` for typed relationships that do not imply ownership.
- `MemoryHandle` for giving agents and services least-privilege access.
- Composable structure facades for graphs, keys, logs, time, lineage, and
  versions.
- Backend protocols for application-selected storage and search systems.
- Policies, hooks, and codecs that stay separate from application behavior.

Genie deliberately does not provide agent execution, planning, embeddings, a
production database, or one universal memory taxonomy.

## Requirements and installation

Genie requires Python 3.11 or newer and has no required runtime dependencies.

From a cloned repository, install the package into the project Conda
environment:

```bash
conda run -n "genie" python -m pip install -e .
```

## Quick start

```python
from genie import InMemoryBackend, Memory


backend = InMemoryBackend()
memory = Memory(backend=backend, principal="agent:planner")

workspace = memory.put(
    type="project",
    value={"name": "Write proposal"},
)
requirement = memory.put(
    parent=workspace,
    type="requirement",
    value="Keep the proposal concise.",
)

# Give the agent access only to this workspace.
agent_memory = memory.get_handle(
    root=workspace,
    principal="agent:planner",
)
agent_memory.put(
    type="observation",
    value="The proposal should use short sections.",
)

results = agent_memory.search("concise")
assert results[0].block.id == requirement.id
```

`InMemoryBackend` is a dependency-free reference backend for examples and
tests. Production applications should supply an adapter with the persistence,
transactions, search, observability, and backup behavior they need.

New blocks are owner-private by default. The example uses the same principal
for `Memory` and its restricted handle.

## Core model

```text
Memory
  project
    requirement
    observation

MemoryLink
  requirement --supports--> observation
```

Parent/child relationships mean ownership. They define subtree scope and
lifecycle. Links express relationships without changing ownership.

Applications own the meaning of block types and values. Keep domain-specific
taxonomies, agent behavior, retrieval strategy, and business rules outside the
core library.

## Composable structures

The optional `genie.structures` facades organize existing blocks:

| Facade | Use it for |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchies |
| `PropertyGraph` | Typed graph relationships and traversal |
| `KeyedCollection` | Namespaced key lookup |
| `EventLog` | Stable append order |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Cycle-protected derivation history |
| `VersionedRecords` | Stable identities and ordered revisions |

Use one facade, combine a subset, or use all seven. They reference the same
blocks rather than copying application values. When a facade receives a
`MemoryHandle`, normal scope and policy checks still apply.

## Architecture at a glance

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

Keep the full `Memory` object and backend in trusted application code. Give
agents rooted handles or application-defined tools exposing only the
operations they need.

## Documentation

- [Architecture overview](docs/design.md)
- [Memory and scoped handles](genie/memory/README.md)
- [Composable structures](genie/structures/README.md)
- [Backend adapters](genie/backend/README.md)
- [Queries and filters](genie/query/README.md)
- [Policies and security](genie/policy/README.md)
- [Hooks](genie/hooks/README.md)
- [Codecs](genie/codecs/README.md)

## Examples

Start with these runnable examples:

- [Basic workflow](examples/basic/basic_usage.py)
- [Scoped agents](examples/basic/scoped_agents.py)
- [Compound agent memory](examples/advanced/agent_memory.py)
- [All seven structures](examples/advanced/experiment_memory.py)

The [examples guide](examples/README.md) lists all basic and advanced examples.
The [lamps](lamps/README.md) are larger demonstrations inspired by existing
memory libraries.

Run an example from the repository root:

```bash
conda run -n "genie" python examples/basic/basic_usage.py
conda run -n "genie" python examples/advanced/experiment_memory.py
```

## Development

Run tests and compilation checks through the project Conda environment:

```bash
conda run -n "genie" python -m pytest
conda run -n "genie" python -m compileall -q genie tests examples lamps
```
