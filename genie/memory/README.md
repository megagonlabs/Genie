# Memory

The `genie.memory` package defines Genie's core data model and public access
objects. It does not define what a memory means or where it is stored.

## Memory blocks

`MemoryBlock` is the smallest independently addressable unit. Applications
choose the block `type`, value shape, metadata, and relationships.

```python
note = memory.put(
    type="preference",
    value={"answer_style": "concise"},
    metadata={"source": "conversation"},
)
```

A block carries:

- a stable id;
- an application-defined `type` and `value`;
- an optional parent id;
- metadata and a content type;
- an optional access policy;
- creator and timestamp information; and
- a version counter for updates to that block.

Create a separate block when a value needs its own identity, policy,
provenance, lifecycle, search behavior, or relationships.

## Ownership and links

Parent/child relationships mean ownership:

```text
project
  task
    note
```

Ownership controls subtree scope, traversal, and recursive deletion. A child
has one parent.

`MemoryLink` represents a relationship that does not imply ownership. Links
are directed, typed, and may carry metadata:

```python
edge = memory.add_link(requirement, proposal, "supports")
edge = memory.update_link(edge, metadata={"confidence": 0.9})
```

Link updates merge metadata while keeping the link id, endpoints, relation,
and creation time unchanged.

Use ownership for containment and lifecycle. Use links for references,
provenance, semantic relationships, and other cross-cutting connections.

## `Memory`

`Memory` is the trusted application interface. It configures:

- one top-level backend;
- the acting principal and policy engine;
- the default policy for new blocks;
- codecs; and
- hooks.

It provides block CRUD, parent traversal, links, mounts, search, and handle
creation. Keep the full `Memory` object in trusted application code.

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="service:memory",
)
root = memory.put(type="project", value={"name": "Atlas"})
```

## `MemoryHandle`

`MemoryHandle` is the restricted interface intended for agents, tools, and
less-trusted components.

```python
planner = memory.get_handle(root=root, principal="agent:planner")
task_agent = planner.get_handle(root=task)
```

A rooted handle keeps normal operations inside its subtree. A child handle can
narrow access but cannot:

- change the principal;
- move its root outside the parent handle;
- enable ancestor, global, mount, or policy-override access that its parent
  lacks; or
- bypass block policies.

Create an unbounded handle only through `Memory.get_unbounded_handle(...)`.
This makes administrative and migration access explicit.

Scope and policy are separate checks. A block must be both inside the handle's
allowed scope and permitted by its `MemoryPolicy`.

## Mounts

A mount is a link with the reserved `genie.mount` relation. It points to
another memory root without making that target a child.

```python
mount = memory.add_mount(local_root, remote_root)
target = memory.resolve_mount(mount)
```

Normal subtree traversal, search, and deletion do not follow mounts. Mount
discovery finds mount links attached inside a subtree but does not enter their
targets. Handle mount operations require `allow_mounts=True`.

## Safety rules

Genie applies these rules around memory operations:

- new blocks are owner-private by default;
- recursive deletion requires write access to every affected block;
- recursive deletion does not follow mounts;
- link results hide unreadable endpoints;
- bounded handles reject out-of-scope references;
- handles cannot choose a block policy unless policy override is enabled;
- returned JSON values and metadata are copied; and
- hooks cannot widen an authorized parent or search scope.

Genie provides access-control building blocks, not a Python sandbox. Expose
application-controlled tools or facades to untrusted agents rather than raw
backends, registries, or the full `Memory` object.

## Related guides

- [Structures](../structures/README.md)
- [Queries](../query/README.md)
- [Policies](../policy/README.md)
- [Backends](../backend/README.md)
- [Hooks](../hooks/README.md)
- [Codecs](../codecs/README.md)
