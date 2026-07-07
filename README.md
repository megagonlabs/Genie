# Genie

Genie is a utility library for developers building their own memory systems. It provides memory blocks, parent/child ownership, and scoped access without tying the application to one agent
platform, database, or memory use case.

Genie is not intended to be a full agent framework or an opinionated long-term memory product. It defines memory blocks, parent/child ownership rules, scoped access rules, and interfaces
developers can implement or plug into for storage, search, access control, observability, and value encoding.

For the full design document, see [docs/design.md](docs/design.md).

## Design Objectives

- Stay platform-agnostic so applications can use Genie from LangChain, LlamaIndex, custom agent runtimes, services, scripts, or other tools.
- Stay storage/database-agnostic so adapters can target Redis, Postgres, document stores, graph databases, vector databases, object stores, or hybrid systems.
- Stay use-case-agnostic so applications can model conversation memory, NL2SQL memory, task/workflow memory, user preferences, domain knowledge, or other application-specific memory.
- Treat parent/child ownership as the default structure for scoped access and recursive operations.
- Keep business logic and agent behavior application-owned.
- Give agents scoped memory access rather than unrestricted access to the full memory system.
- Keep storage, traversal, search, policy, hooks, and value encoding separable.
- Keep the core package small and avoid unnecessary dependencies.

## Out of Scope

- Require or favor one specific database.
- Include a built-in production storage engine in the core package.
- Implement embeddings or semantic search by default.
- Manage agent execution, planning, or tool calling.
- Own application business logic or system design.
- Define one universal memory structure.

## Quick Start

```python
from genie import InMemoryBackend, Memory

memory = Memory(backend=InMemoryBackend())

root = memory.put(type="task", value={"name": "write proposal"})
note = memory.put(
    parent=root,
    type="note",
    value="User prefers database-agnostic memory building blocks.",
)

worker_memory = memory.get_handle(root=root, principal="agent:worker")
worker_memory.put(type="observation", value="The task has one design note.")

results = worker_memory.search("database-agnostic")
assert results[0].block.id == note.id
```

`Memory()` defaults to `InMemoryBackend()` when no backend is provided. Examples pass `backend=InMemoryBackend()` explicitly so first-time readers can see where storage comes from.

## Core Concepts

- `Memory`: The main interface for a Genie memory system. It configures the backend, codecs, hooks, policy engine, and default principal.
- `MemoryHandle`: A restricted handle to part of a `Memory` object. Agents and sub-agents should usually receive a handle instead of the full `Memory` object.
- `MemoryBlock`: The smallest memory unit Genie stores and retrieves. A block has an id, `type`, `value`, metadata, optional policy, and optional parent.
- `MemoryLink`: A typed relationship between two blocks that does not make one block own the other.
- `MemoryBackend`: The adapter interface for persistence, traversal, links, and search.
- `MemoryHook`: User-provided code that runs before or after memory operations.
- `MemoryCodec`: A converter between application values and storage-safe data.

Genie models memory as a typed graph of blocks. Parent/child relationships show ownership for scoped access. Links and mounts connect blocks without making one own the other. The `type` field
and the meaning of `value` are application-defined. Codecs convert values between application objects and storage-safe data.

## Examples

Focused runnable examples live in [examples](examples):

- `crud_blocks.py`: CRUD, updating, delete, and child traversal.
- `scoped_agents.py`: top-level agents and sub-agents using `MemoryHandle`.
- `search_filters.py`: text search, types, scopes, and metadata filters.
- `links.py`: typed links, symbolic mounts, and linked-memory search.
- `routed_backend.py`: route blocks across multiple backend stores.
- `policies.py`: principal and role based access control.
- `hooks_codecs.py`: hooks around memory operations with custom and stdlib value codecs.
- `stdlib_codecs.py`: opt-in stdlib codecs with decoded and raw payload examples.
- `basic_usage.py`: integrated workflow that combines the main features.

Run one directly from the repository root:

```bash
conda run -n "genie" python examples/basic_usage.py
```

## Scoped Access

Sub-agents should receive a `MemoryHandle`, not the whole memory object. A handle supports normal CRUD, traversal, link, mount, search, and child handle operations while constraining access to
the handle root unless explicitly configured otherwise.

```python
subtask_memory = memory.get_handle(root=root)
subtask_memory.search("style")

context_memory = memory.get_handle(root=root, allow_up=True)
context_memory.search("project", scope="ancestors")
```

A child handle can narrow access but cannot increase it. A child handle cannot change principal identity, add ancestor traversal, add global search, add mount access, or move its root outside
the parent handle's subtree unless the parent handle already allows that access.

`Memory.get_handle(root=...)` requires a concrete root. Unbounded handles are created with `Memory.get_unbounded_handle(...)` so administrative, routing, migration, or other trusted workflows
are explicit at the call site.

Mount operations on a `MemoryHandle` are available only when the handle is created with `allow_mounts=True`.

## Search and Filters

Search accepts text, scope, block type filters, metadata filters, and result limits:

```python
results = memory.search(
    "concise",
    root=root,
    scope="subtree",
    types=["requirement"],
    filters={
        "metadata.priority": {">=": 3},
        "owner": {"in": ["team", "agent:planner"]},
    },
)
```

Application-facing filters can use human-friendly operators. Genie normalizes them before they reach backend adapters.

| Friendly | Canonical |
| :-- | :-- |
| `==` | `$eq` |
| `=` | `$eq` |
| `!=` | `$ne` |
| `>` | `$gt` |
| `>=` | `$gte` |
| `<` | `$lt` |
| `<=` | `$lte` |
| `in` | `$in` |

## Backends

Users configure one top-level backend object:

```python
memory = Memory(backend=InMemoryBackend())
```

The included `InMemoryBackend` is the reference backend for tests, examples, and local prototypes. Production deployments should provide a backend adapter with the desired persistence,
transactions, observability, and backup behavior.

Backend adapters implement the smaller interfaces in `genie.backend`:

- `BlockStore`
- `TraversalIndex`
- `SearchIndex`
- `LinkStore`

That keeps stores, traversal, search, and links separable internally while still allowing users to configure one top-level backend.

For sharding, data residency, migrations, or customer-owned storage, `RoutedBackend` can coordinate multiple backend routes behind the same public `MemoryBackend` interface. Each block still
has exactly one route that owns it. By default, children stay on the same route as their parent; cross-route references should be modeled with links or mounts rather than parent/child
ownership.

Routes can be added at runtime when an application discovers a mount or tenant descriptor for a backend that was not opened at startup:

```python
backend.register_route("eu-prod", eu_backend)
mounted_root = memory.resolve_mount(mount)
```

The public `memory.add_mount()` call validates both endpoints. Pending mounts to a route that has not been opened yet are represented by adapter or import code in a shared `LinkStore`, then
resolved after the route is registered.

## Policies

`Principal` is the acting identity for an operation. `MemoryPolicy` controls read, write, search, traversal, and application-defined custom operations.

```python
from genie import AccessRule, Memory, MemoryPolicy

memory = Memory()
root = memory.put(type="project", value="proposal")
private_note = memory.put(
    parent=root,
    type="note",
    value="compliance-only detail",
    policy=MemoryPolicy(read=AccessRule.only(roles={"compliance"})),
)
```

If no policy is attached to a block, the block is public. Scope limits are enforced in addition to block policies.

## Hooks

Hooks customize behavior before and after memory operations. They are useful for audit logs, metrics, tracing, redaction, or external indexing.

```python
from genie import InMemoryBackend, Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(backend=InMemoryBackend(), hooks=[AuditHook()])
```

Hooks run inside the public operation flow. Genie keeps structure and access checks around hook execution, so hooks cannot bypass scope or policy checks.

## Codecs

Codecs translate user-level Python objects into storage-safe values and back. `Memory()` registers only `JsonCodec` by default. Additional standard-library codecs are opt-in:

```python
from decimal import Decimal

from genie import InMemoryBackend, Memory
from genie.codecs import CodecRegistry, JsonCodec
from genie.codecs.stdlib import DatetimeCodec, DecimalCodec, UUIDCodec

memory = Memory(
    backend=InMemoryBackend(),
    codecs=CodecRegistry([DatetimeCodec(), DecimalCodec(), UUIDCodec(), JsonCodec()]),
)

memory.put(type="amount", value=Decimal("0.1000000000000000001"))
```

The stdlib module includes `BytesCodec`, `DatetimeCodec`, `DecimalCodec`, `UUIDCodec`, `DataclassCodec`, and `EnumCodec`. Register dataclass and enum types explicitly so decoding only
reconstructs trusted application types.

## Operational Safety

Genie uses cautious behavior:

- child handles cannot increase access
- bounded handles require an existing root block
- unbounded handles must be created explicitly and require an explicit root for non-global search
- recursive delete requires write access to every affected block
- recursive delete does not follow symbolic mounts
- `before_put` hooks cannot change parent placement after authorization
- search hooks are re-authorized and results are re-filtered by policy and scope
- returned JSON values and metadata are copied
- in-memory reparenting rejects cycles
- link listing hides relationships whose endpoints are not readable by the principal
- mount discovery does not follow mounted target trees
- JSON encoding rejects `NaN` and `Infinity`
- incompatible filter comparisons produce no matches

## Development

Run Python through the project Conda environment:

```bash
conda run -n "genie" python -m compileall -q genie tests examples
conda run -n "genie" python -m pytest
```
