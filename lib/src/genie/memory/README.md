# Memory

`genie.memory` defines the data model of Genie and its public access objects.
The model does not depend on a storage engine. Your application decides what
the stored values mean.

## Blocks, ownership, and links

**`MemoryBlock`** is the smallest value that you can address on its own. Fields:
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

- **A value gets its own block** when it needs its own identity, policy, provenance (a record of where it came from), lifecycle, search behavior, or relationships.
- **Parent and child means ownership.** A child has exactly one parent. The tree controls subtree scope, traversal, and recursive delete.
- **`MemoryLink` means relationship.** It is directed and typed, and it does not imply ownership.

```python
edge = memory.add_link(requirement, proposal, "supports")
edge = memory.update_link(edge, metadata={"confidence": 0.9})
```

- **`update_link` merges metadata.** It keeps the id, endpoints, relation, and creation time.
- **Choice by intent:** ownership for containment and lifecycle. Links for references, provenance, and relationships across the tree.
- **The whole backend shares relation names.** One name means the same relation for all blocks, tenants, and handles. A prefix of your own, such as `"billing.derived_from"`, keeps the names apart when 2 parts of an application can choose the same name.

## Trusted and restricted access

**`Memory`** is the trusted interface. It belongs inside trusted application code.

- **Holds:** one backend, `Principal`, `PolicyEngine`, `default_policy`, `CodecRegistry`, `HookRegistry`, and the `attributes` that it reports to hooks.
- **Offers:** block CRUD, traversal, links, mounts, search, access checks, and handle creation.
- **`hooks` is read-only.** To change which hooks run, you edit that registry. You cannot replace it, so nobody can detach a mandatory hook. See [hooks](../hooks/README.md#the-registry).

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

- **Bounded handle:** has a `root`. By default, it reaches only that root and the blocks below it. `allow_up` adds ancestors, `allow_global` adds global search, and `allow_mounts` adds mounts.
- **Unbounded handle:** has no `root`. Only `Memory.get_unbounded_handle(...)` creates one.
- **A child handle can only narrow access.** It cannot change its principal, move its root outside the parent, turn on access that the parent turned off, or skip policies.
- **Scope and policy are separate checks.** A block is inside the allowed scope of the handle, and the `MemoryPolicy` of the block also allows the operation. Both checks run.
- **Each handle has its own `id`.** It reports its root and its enabled capabilities to hooks through `attributes`. So when several handles share one principal, an audit trail still shows which sandbox did the work. See [hooks](../hooks/README.md#scopes-and-attributes).

## Mounts

A `genie.mount` link points at another root, and the root does not become a
child:

```python
mount = memory.add_mount(local_root, remote_root)
target = memory.resolve_mount(mount)
```

- **Only `add_mount` creates one.** `add_link` and `add_acyclic_link` raise `ValueError` for the `genie.mount` relation.
- **Not followed** by subtree traversal, subtree search, or delete.
- **`links` search:** a `links` search counts a mount as a link. So it finds the mount target itself, but not the blocks below the target.
- **Discovery** finds mount links, but it does not go into their targets.
- **Handle access** needs `allow_mounts=True`.
- **A mount target can be outside the handle root,** if the principal of the handle can read it. So `allow_mounts` lets a handle read each block that its principal can read, if it knows the block id and can write a block inside its root.
- **A hidden target stays hidden.** A rooted handle refuses a missing target and an unreadable target outside its root with the same `AccessDenied`. So `add_mount` does not show if that block exists.

## Safety rules

- **Default policy** is `MemoryPolicy.owner_private()`. So only the creator can `read`, `write`, `search`, `traverse_up`, and `traverse_down` a new block.
- **Recursive delete** needs `write` on the target block only.
- **A change of a block in place** with `set` or `update` needs `read` and `write`, because both return the stored block. `update_link` needs `read` on the source for the same reason.
- **Structure membership** changes need `write` on the container block. Facades check it with `require_access`, which runs one policy check and changes nothing.
- **Stale writes fail.** A block update fails on a stale `version`, and a delete fails if the subtree changed. Both raise `MemoryConflict`.
- **Links are hidden** when an endpoint does not allow `read`.
- **A bounded handle** rejects references outside its scope.
- **A policy choice** from a handle needs `allow_policy_override=True`.
- **A caller-provided `id`** from a handle needs `allow_custom_ids=True`. Block ids are unique in the whole backend. So a handle with this flag can find out if an id exists outside its root, and it can take an id before trusted code does.
- **Ids, block types, and relations** are non-empty strings. A wrong type raises `TypeError`, and an empty string or a string with only whitespace raises `ValueError`. A bad value never gets to the backend.
- **Metadata** on blocks and links is a strict JSON dictionary. Other metadata raises `TypeError`.
- **JSON values and metadata nest at most `MAX_NESTING_DEPTH` (100) levels.** Deeper data raises `ValueError` before the backend runs.
- **Returned JSON values and metadata are defensive copies.** You get a copy, so a change to what you got does not change what is stored.
- **Hooks cannot widen** a parent or search scope that was already authorized.
- **Nested hook operations** are allowed. The registry skips a callback that is already active when it starts again, and the depth of nested operations has a limit.

Genie gives you parts for access control, not a Python sandbox. Untrusted
agents get bounded tools or facades. They never get raw backends or registries.

Related: [Structures](../structures/README.md), [Queries](../query/README.md),
[Policies](../policy/README.md), [Backends](../backend/README.md),
[Hooks](../hooks/README.md), and [Codecs](../codecs/README.md).
