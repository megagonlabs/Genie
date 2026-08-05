# Genie

Genie is a utility library for developers building their own memory systems. It provides memory blocks, parent/child ownership, and scoped access without tying the application to one agent
platform, database, or memory use case.

Genie is not intended to be a full agent framework or an opinionated long-term memory product. It defines memory blocks, parent/child ownership rules, scoped access rules, and interfaces
developers can implement or plug into for storage, search, access control, observability, and value encoding.

For the architecture overview and package-specific design guides, see
[docs/design.md](docs/design.md).

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

memory = Memory(backend=InMemoryBackend(), principal="agent:worker")

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

`Memory()` defaults to `InMemoryBackend()` when no backend is provided. New blocks are owner-private by default, so the quick start uses the same principal for the full `Memory` object and the
bounded handle. Examples pass `backend=InMemoryBackend()` explicitly so first-time readers can see where storage comes from.

## Core Concepts

- `Memory`: The main interface for a Genie memory system. It configures the backend, codecs, hooks, policy engine, default principal, and default policy.
- `MemoryHandle`: A restricted handle to part of a `Memory` object. Agents and sub-agents should usually receive a handle instead of the full `Memory` object.
- `MemoryBlock`: The smallest memory unit Genie stores and retrieves. A block has an id, `type`, `value`, metadata, optional policy, and optional parent.
- `MemoryLink`: A typed relationship between two blocks that does not make one block own the other.
- `MemoryBackend`: The adapter interface for persistence, traversal, links, and search.
- `MemoryHook`: User-provided code that runs before or after memory operations.
- `MemoryCodec`: A converter between application values and storage-safe data.

Genie models memory as a typed graph of blocks. Parent/child relationships show ownership for scoped access. Links and mounts connect blocks without making one own the other. The `type` field
and the meaning of `value` are application-defined. Codecs convert values between application objects and storage-safe data.

## Structure Foundations

The focused `genie.structures` package provides shared access protocols and immutable records for optional higher-level structures. `MemoryAccess` describes the common block, tree, link, and
search surface implemented by both `Memory` and `MemoryHandle`. Narrower protocols let a structure depend only on the operations it uses.

Structural records describe membership without adding optional fields to every `MemoryBlock`:

- `MemoryAddress` and `KeyBinding` describe namespaced keys.
- `LogEntry` describes a stable position in an ordered log.
- `ValidityInterval`, `TemporalEntry`, and `TemporalItem` describe event time,
  validity periods, and resolved temporal members.
- `GraphNeighbor` and `GraphPath` describe graph traversal through memory links.
- `RevisionEntry` and `RevisionItem` separate logical record revisions from a
  block's update counter.

These records validate structure data but do not persist or query it by themselves. Structure facades and optional backend capabilities build on them.

`OwnershipTree` is the first facade. It provides a rooted view over Genie's existing parent/child behavior:

```python
from genie import Memory
from genie.structures import OwnershipTree


memory = Memory(principal="agent:planner")
root = memory.put(type="project", value="proposal")
tree = OwnershipTree(memory, root=root)

task = tree.create_child(type="task", value="draft outline")
note = tree.create_child(parent=task, type="note", value="Keep it short.")

assert tree.children(task) == [note]
assert tree.ancestors(note) == [task, root]
```

The facade never traverses above its configured root. When constructed with a `MemoryHandle`, it also keeps the handle's scope and policy restrictions.

`PropertyGraph` treats blocks as nodes and existing typed links as directed edges:

```python
from genie.structures import PropertyGraph


graph = PropertyGraph(memory)
edge = graph.add_edge(task, note, "supported_by", metadata={"confidence": 0.9})

neighbors = graph.neighbors(task, relation="supported_by")
paths = graph.walk(task, max_depth=2, limit=20)
```

Property graphs may contain cycles and parallel edges. `walk` returns bounded simple paths in breadth-first order, so traversal terminates without changing the stored graph. Pass a scoped
`MemoryHandle` instead of `Memory` when graph operations must remain inside an agent's memory boundary.

`LineageDAG` uses the same links while protecting one relation from directed
cycles:

```python
from genie.structures import LineageDAG


lineage = LineageDAG(memory, relation="derived_into")
lineage.add(source_note, summary, metadata={"method": "summarize"})
lineage.add(summary, report)

predecessors = lineage.predecessors(report)
paths = lineage.walk_predecessors(report, max_depth=3)
```

Edges point from predecessor to successor. The backend checks reachability and
inserts the edge atomically. The exact relation identifies one DAG; use
different relation names for independent lineage graphs.

`KeyedCollection` and `EventLog` attach existing blocks to atomic
backend-managed structures:

```python
from genie import InMemoryBackend, Memory
from genie.structures import EventLog, KeyedCollection, MemoryAddress


backend = InMemoryBackend()
memory = Memory(backend=backend, principal="agent:planner")
preferences_root = memory.put(type="collection", value="preferences")
events_root = memory.put(type="log", value="events")
language = memory.put(type="preference", value="English")

preferences = KeyedCollection(memory, backend, collection=preferences_root)
preferences.bind(MemoryAddress(("users", "alice"), "language"), language)

events = EventLog(memory, backend, log=events_root)
entry = events.append(language)
assert entry.entry.sequence == 0
```

Key binding is create-only. `rebind(..., expected=...)` and `unbind(..., expected=...)` use compare-and-swap checks so concurrent writers cannot silently overwrite each other. Event-log append
atomically allocates one stable, increasing sequence number per log. The included `InMemoryBackend` implements both optional capabilities. Other backends implement only the structures they
support.

`TemporalCollection` attaches event times and validity periods without copying
block values:

```python
from datetime import UTC, datetime

from genie.structures import TemporalCollection, ValidityInterval


timeline_root = memory.put(type="temporal_collection", value="facts")
timeline = TemporalCollection(memory, backend, collection=timeline_root)
timeline.add(
    language,
    occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
    validity=ValidityInterval(datetime(2026, 1, 1, tzinfo=UTC)),
)

current = timeline.valid_at(datetime(2026, 2, 1, tzinfo=UTC))
```

Temporal queries use half-open bounds. A backend needs only `TemporalStore`;
the facade can scan it in bounded pages. Backends may additionally implement
`TemporalIndex` to prefilter candidates. Both paths return the same stable,
access-filtered results.

`VersionedRecords` composes keyed identity with per-record revision ordering:

```python
from genie.structures import MemoryAddress, VersionedRecords


versions_root = memory.put(type="versioned_collection", value="preferences")
identity = memory.put(type="record_identity", value="favorite color")
blue = memory.put(type="record_revision", value="blue")
green = memory.put(type="record_revision", value="green")
address = MemoryAddress(("users", "alice"), "favorite_color")

versions = VersionedRecords(memory, backend, collection=versions_root)
versions.register(address, identity)
versions.append(address, blue)   # revision 1
versions.append(address, green)  # revision 2

current = versions.current(address)
history = versions.history(address)
```

The key always points to the stable identity block; revision blocks remain
separate. Revision numbering is atomic per identity and is independent from
`MemoryBlock.version`.

Link identity and topology remain stable after creation. Applications can merge new relationship properties with `memory.update_link(link, metadata={...})`; the method persists and returns a
replacement `MemoryLink` with the same id, source, target, relation, and creation timestamp.

## Examples

Basic runnable examples live in [examples/basic](examples/basic):

- `basic/crud_blocks.py`: CRUD, updating, delete, and child traversal.
- `basic/scoped_agents.py`: top-level agents and sub-agents using `MemoryHandle`.
- `basic/search_filters.py`: text search, types, scopes, and metadata filters.
- `basic/links.py`: typed links, symbolic mounts, and linked-memory search.
- `basic/routed_backend.py`: route blocks across multiple backend stores.
- `basic/policies.py`: principal and role based access control.
- `basic/hooks_codecs.py`: hooks around memory operations with custom and stdlib value codecs.
- `basic/stdlib_codecs.py`: opt-in stdlib codecs with decoded and raw payload examples.
- `basic/basic_usage.py`: integrated workflow that combines the main features.

Advanced composition examples live in [examples/advanced](examples/advanced):

- `temporal_collection.py`: one temporal structure by itself.
- `agent_memory.py`: an agent with keyed preconditions, experiment
  history, and graph knowledge in one compound memory.
- `experiment_memory.py`: all seven optional structures in one
  experiment-memory workflow.

Run one directly from the repository root:

```bash
conda run -n "genie" python examples/basic/basic_usage.py
conda run -n "genie" python examples/advanced/experiment_memory.py
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

A child handle can narrow access but cannot increase it. A child handle cannot change principal identity, add ancestor traversal, add global search, add mount access,
add policy override access, or move its root outside the parent handle's subtree unless the parent handle already allows that access.

`Memory.get_handle(root=...)` requires a concrete root, and the requested handle principal must be able to read that root. Unbounded handles are created with
`Memory.get_unbounded_handle(...)` so administrative, routing, migration, or other trusted workflows are explicit at the call site.

Mount operations on a `MemoryHandle` are available only when the handle is created with `allow_mounts=True`.

Handles cannot pass explicit block policies to `put(...)` unless they are created with `allow_policy_override=True`. Without that option, blocks created through a handle use the memory's
configured `default_policy`. This keeps untrusted agent code from making its own writes public or granting access to other principals.

Handle default scopes are checked when the handle is created. For example, `default_scope="global"` requires `allow_global=True`, and `default_scope="ancestors"` requires `allow_up=True`.

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

Optional structure facades use separate backend capabilities:

- `AcyclicLinkStore`: atomic cycle check and link insertion for lineage DAGs.
- `KeyedStore`: atomic unique key binding and compare-and-swap replacement.
- `SequenceStore`: atomic monotonic sequence allocation for append-only logs.
- `TemporalStore`: temporal membership persistence and portable paging.
- `TemporalIndex`: optional temporal query prefiltering.

These optional protocols are not part of the required `MemoryBackend` interface.
`VersionedStore` simply combines the existing `KeyedStore` and `SequenceStore`
protocols; it adds no backend operation.

The facades are composable views over the same blocks. A block can belong to an
ownership tree, graph, keyed collection, event log, temporal collection,
lineage DAG, and version history at the same time. `LineageDAG` stores ordinary
property-graph links with atomic cycle protection. `VersionedRecords` uses the
same key bindings and log entries that `KeyedCollection` and `EventLog` expose.

When a facade receives a `MemoryHandle`, it resolves every referenced block
through that handle, so scope and block policies still apply. Possession of a
configured facade or tool grants access to its structural methods. Keep raw
backend capabilities in trusted application code, and expose only the facade
methods an agent needs.

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

memory = Memory(default_policy=MemoryPolicy.owner_private())
root = memory.put(type="project", value="proposal")
public_note = memory.put(
    parent=root,
    type="note",
    value="Safe to share broadly",
    policy=MemoryPolicy.public(),
)
compliance_note = memory.put(
    parent=root,
    type="note",
    value="compliance-only detail",
    policy=MemoryPolicy(
        read=AccessRule.only(roles={"compliance"}, owner=True),
        search=AccessRule.only(roles={"compliance"}, owner=True),
        traverse_up=AccessRule.only(roles={"compliance"}, owner=True),
        traverse_down=AccessRule.only(roles={"compliance"}, owner=True),
        write=AccessRule.owner(),
    ),
)
```

`MemoryPolicy.owner_private()` is the default for new blocks when `default_policy` is not provided. Applications can choose another default, such as `MemoryPolicy.public()`, but production code
should keep private defaults and attach public or shared policies only where broader access is intended. Scope limits are enforced in addition to block policies.

For managed memory systems, use `owner_or(...)` so the block creator keeps access while a supervisor principal or admin role can also access new blocks:

```python
memory = Memory(
    principal="service:memory",
    default_policy=MemoryPolicy.owner_or(roles={"memory_admin"}),
)
```

## Security Model

Genie provides access-control building blocks, not a Python sandbox. Keep the full `Memory` object, backend adapters, hooks, codecs, and raw indexes in trusted application code. Pass
`MemoryHandle` objects to agents or sub-agents through application-controlled tool interfaces.

Production systems should use explicit principals for users, agents, services, and supervisors. The default principal is useful for simple scripts, but shared systems should pass a concrete
principal when creating `Memory` and when creating handles.

By default, new blocks are owner-private and handle writes cannot override policies. Use `MemoryPolicy.owner_or(...)` when the creator and a supervisor principal or role both need access. Use
`allow_policy_override=True` only for trusted handles that are allowed to choose block-level policies.

Denied reads raise `AccessDenied` while missing blocks return `None` through the full `Memory` API. If an application must hide whether a block exists, wrap Genie operations at the
application boundary and return a generic not-found response for both missing and denied reads. Applications should also avoid letting untrusted callers choose block ids because duplicate-id
errors can reveal that an id already exists.

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
- handle default scopes cannot require disabled access
- handles cannot set explicit block policies unless created with `allow_policy_override=True`
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
