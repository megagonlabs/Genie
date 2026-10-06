# Memory

`genie.memory` defines Genie's data model and its public access objects. The
model does not depend on any storage engine. Your application decides what the
stored values mean.

## Blocks, ownership, and links

**`MemoryBlock`** is the smallest value you can address on its own. Fields:
stable `id`, application-defined `type` and `value`, optional `parent_id`,
`metadata`, `content_type`, `policy`, the creator in `created_by`, the
`created_at` and `updated_at` timestamps, and a `version`.

```python
note = memory.put(
    type="preference",
    value={"answer_style": "concise"},
    metadata={"source": "conversation"},
)
```

- **Make a value its own block** when it needs its own identity, policy, provenance (a record of where it came from), lifecycle, search behavior, or relationships.
- **Parent/child means ownership.** A child has exactly one parent, and the tree controls subtree scope, traversal, and recursive deletion.
- **`MemoryLink` means relationship.** It is directed and typed, and it does not imply ownership.

```python
edge = memory.add_link(requirement, proposal, "supports")
edge = memory.update_link(edge, metadata={"confidence": 0.9})
```

- **`update_link` merges metadata** and keeps the id, endpoints, relation, and creation time.
- **Pick by intent:** ownership for containment and lifecycle; links for references, provenance, and relationships that cut across the tree.
- **Relation names are shared across the whole backend.** One name means the same relation for every block, tenant, and handle. Add your own prefix, such as `"billing.derived_from"`, when two parts of an application could pick the same name.

## Trusted and restricted access

**`Memory`** is the trusted interface. Keep it inside trusted application code.

- **Holds:** one backend, `Principal`, `PolicyEngine`, `default_policy`, `CodecRegistry`, `HookRegistry`, and the `attributes` it reports to hooks.
- **Offers:** block CRUD, traversal, links, mounts, search, and handle creation.
- **`hooks` is read-only.** You change which hooks run by editing that registry, not by replacing it, so nobody can detach a mandatory hook. See [hooks](../hooks/README.md#the-registry).

```python
from genie import InMemoryBackend, Memory


memory = Memory(backend=InMemoryBackend(), principal="service:memory")
root = memory.put(type="project", value={"name": "Atlas"})
```

**`MemoryHandle`** is the restricted interface for agents, tools, and services:

```python
planner = memory.get_handle(root=root, principal="agent:planner")
task_agent = planner.get_handle(root=task)
```

- **Bounded handle:** has a `root`, and reaches that root and the blocks below it, and nothing else.
- **Unbounded handle:** has no `root`. Only `Memory.get_unbounded_handle(...)` creates one.
- **A child handle narrows only.** It cannot change its principal, move its root outside the parent, turn on access that the parent turned off, or skip policies.
- **Scope and policy are checked separately.** A block must be inside the handle's allowed scope, and the block's `MemoryPolicy` must also allow the operation.
- **Every handle has its own `id`,** and reports its root and its enabled capabilities to hooks through `attributes`. So when several handles share one principal, an audit trail still shows which sandbox did the work. See [hooks](../hooks/README.md#scopes-and-attributes).

## Mounts

A `genie.mount` link points at another root without making it a child:

```python
mount = memory.add_mount(local_root, remote_root)
target = memory.resolve_mount(mount)
```

- **Not followed** by subtree traversal, search, or deletion.
- **Discovery** finds mount links but does not enter their targets.
- **Handle access** needs `allow_mounts=True`.

## Safety rules

- **Default policy** is `MemoryPolicy.owner_private()`, so only the creator can `read`, `write`, `search`, `traverse_up`, and `traverse_down` a new block.
- **Recursive deletion** needs `write` on the target block only.
- **Stale writes fail.** A block update fails on a stale `version`, and a deletion fails if the subtree changed. Both raise `MemoryConflict`.
- **Links hide** when an endpoint does not allow `read`.
- **A bounded handle** rejects references outside its scope.
- **Choosing a policy** from a handle needs `allow_policy_override=True`.
- **Metadata** on blocks and links must be strict JSON dictionaries.
- **Returned JSON values and metadata are defensive copies.** You get a copy, so changing what you received does not change what is stored.
- **Hooks cannot widen** a parent or search scope that was already authorized.
- **Nested hook operations** are allowed. Callbacks that are already running are skipped on reentry, and nesting has a limit.

Genie gives you access-control building blocks, not a Python sandbox. Give
untrusted agents bounded tools or facades. Never give them raw backends or
registries.

Related: [Structures](../structures/README.md), [Queries](../query/README.md),
[Policies](../policy/README.md), [Backends](../backend/README.md),
[Hooks](../hooks/README.md), and [Codecs](../codecs/README.md).
