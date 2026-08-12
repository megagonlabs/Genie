# Memory

`genie.memory` defines Genie's storage-independent data model and public access
objects. Applications decide what stored values mean.

## Blocks, ownership, and links

`MemoryBlock` is the smallest independently addressable value. It carries a
stable id, application-defined type and value, optional parent, metadata,
content type, policy, creator, timestamps, and version.

```python
note = memory.put(
    type="preference",
    value={"answer_style": "concise"},
    metadata={"source": "conversation"},
)
```

Create a separate block when a value needs its own identity, policy,
provenance, lifecycle, search behavior, or relationships.

Parent/child relationships mean ownership: a child has one parent, and the
tree controls subtree scope, traversal, and recursive deletion. `MemoryLink`
represents a directed, typed, non-owning relationship:

```python
edge = memory.add_link(requirement, proposal, "supports")
edge = memory.update_link(edge, metadata={"confidence": 0.9})
```

Link updates merge metadata while preserving id, endpoints, relation, and
creation time. Use ownership for containment and lifecycle; use links for
references, provenance, and cross-cutting relationships.

## Trusted and restricted access

`Memory` configures one backend, principal, policy engine, default policy,
codecs, and hooks. It provides block CRUD, traversal, links, mounts, search,
and handle creation. Keep it in trusted application code.

```python
from genie import InMemoryBackend, Memory


memory = Memory(backend=InMemoryBackend(), principal="service:memory")
root = memory.put(type="project", value={"name": "Atlas"})
```

`MemoryHandle` is the restricted interface for agents, tools, and services:

```python
planner = memory.get_handle(root=root, principal="agent:planner")
task_agent = planner.get_handle(root=task)
```

A child handle may narrow access but cannot change its principal, move its root
outside the parent, enable access disabled by the parent, or bypass policies.
Only `Memory.get_unbounded_handle(...)` creates an administrative handle.

Scope and policy are independent: a block must be inside the handle's allowed
scope and permitted by its `MemoryPolicy`.

## Mounts

A `genie.mount` link points to another root without making it a child:

```python
mount = memory.add_mount(local_root, remote_root)
target = memory.resolve_mount(mount)
```

Subtree traversal, search, and deletion do not follow mounts. Discovery finds
mount links without entering their targets. Handle mount operations require
`allow_mounts=True`.

## Safety rules

- New blocks are owner-private by default.
- Recursive deletion requires write access to every checked block.
- Block updates reject a stale version; deletion rejects a changed subtree.
- Links with unreadable endpoints are hidden.
- Bounded handles reject out-of-scope references.
- Handles need policy override permission to choose a new block's policy.
- Returned JSON values and metadata are defensive copies.
- Hooks cannot widen an authorized parent or search scope.
- Hook callbacks may perform nested memory operations; active callbacks are
  skipped on reentry and nesting is bounded.

Genie supplies access-control building blocks, not a Python sandbox. Give
untrusted agents bounded tools or facades, never raw backends or registries.

Related: [Structures](../structures/README.md), [Queries](../query/README.md),
[Policies](../policy/README.md), [Backends](../backend/README.md),
[Hooks](../hooks/README.md), and [Codecs](../codecs/README.md).
