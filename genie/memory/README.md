# Memory

`genie.memory` defines Genie's data model and its public access objects. The
model does not depend on any storage engine. Your application decides what the
stored values mean.

## Blocks, ownership, and links

`MemoryBlock` is the smallest value you can address on its own. It holds a
stable id, a type and a value that your application defines, an optional
parent, metadata, a content type, a policy, a creator, timestamps, and a
version.

```python
note = memory.put(
    type="preference",
    value={"answer_style": "concise"},
    metadata={"source": "conversation"},
)
```

Make a value its own block when it needs its own identity, policy, provenance
(a record of where it came from), lifecycle, search behavior, or relationships.

A parent/child relationship means ownership. A child has exactly one parent,
and the tree controls subtree scope, traversal, and recursive deletion. A
`MemoryLink` is a directed, typed relationship that does not mean ownership:

```python
edge = memory.add_link(requirement, proposal, "supports")
edge = memory.update_link(edge, metadata={"confidence": 0.9})
```

Updating a link merges its metadata and keeps the id, endpoints, relation, and
creation time. Use ownership for containment and lifecycle. Use links for
references, provenance, and relationships that cut across the tree.

## Trusted and restricted access

`Memory` holds one backend, principal, policy engine, default policy, set of
codecs, set of hooks, and the `attributes` it reports to hooks. It offers block
CRUD, traversal, links, mounts, search, and handle creation. Keep it inside
trusted application code.

`hooks` is read-only. You change which hooks run by editing that registry, not
by replacing it, so nobody can detach a mandatory hook. See
[hooks](../hooks/README.md#the-registry).

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

A handle with a root is *bounded*: it reaches that root and the blocks below it,
and nothing else. A handle with no root is *unbounded*, and only
`Memory.get_unbounded_handle(...)` creates one.

A child handle can narrow access, but it cannot change its principal, move its
root outside the parent, turn on access that the parent turned off, or skip
policies.

Scope and policy are checked separately. A block must be inside the handle's
allowed scope, and the block's `MemoryPolicy` must also allow the operation.

Every handle has its own `id`. Through `attributes` it reports its root and its
enabled capabilities to hooks. So when several handles share one principal, an
audit trail still shows which sandbox did the work. See
[hooks](../hooks/README.md#scopes-and-attributes).

## Mounts

A `genie.mount` link points at another root without making it a child:

```python
mount = memory.add_mount(local_root, remote_root)
target = memory.resolve_mount(mount)
```

Subtree traversal, search, and deletion do not follow mounts. Discovery finds
mount links but does not enter their targets. Mount operations on a handle need
`allow_mounts=True`.

## Safety rules

- By default, only the creator of a new block can read it.
- Recursive deletion needs write access to every block it checks.
- A block update fails on a stale version. A deletion fails if the subtree
  changed.
- Links are hidden when an endpoint cannot be read.
- A bounded handle rejects references outside its scope.
- A handle needs policy override permission to choose the policy of a new
  block.
- Block and link metadata must be strict JSON dictionaries.
- Returned JSON values and metadata are defensive copies. You get a copy, so
  changing what you received does not change what is stored.
- Hooks cannot widen a parent or search scope that was already authorized.
- A hook callback may run nested memory operations. Callbacks that are already
  running are skipped on reentry, and nesting has a limit.

Genie gives you access-control building blocks, not a Python sandbox. Give
untrusted agents bounded tools or facades. Never give them raw backends or
registries.

Related: [Structures](../structures/README.md), [Queries](../query/README.md),
[Policies](../policy/README.md), [Backends](../backend/README.md),
[Hooks](../hooks/README.md), and [Codecs](../codecs/README.md).
