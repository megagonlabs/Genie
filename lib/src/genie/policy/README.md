# Policies

`genie.policy` gives access rules for single blocks. A request through a handle
passes only if the handle scope and the `MemoryPolicy` of the block both allow
it.

## Principals and rules

- **`Principal`** identifies a user, an agent, or a service by `id`, `roles`, and optional `metadata`.
- **Ids and role names** are non-empty strings. Principal input that is not valid raises an error. It never falls back to the default `system` identity.
- **Rules check and copy their input.** Policy rules check the types of their fields and copy each mutable input set.

| `AccessRule` | Who passes |
| --- | --- |
| `AccessRule.allow()` | Everyone |
| `AccessRule.deny()` | Nobody |
| `AccessRule.owner()` | The block creator |
| `AccessRule.only(...)` | Chosen `principals`, chosen `roles`, or those plus `owner=True` |

- **Defaults fail closed.** `AccessRule()` denies everyone. A rule made directly from `principals`, `roles`, or `allow_owner` allows only those.
- **`allow_all=True` cannot go with** `principals`, `roles`, or `allow_owner`. That combination raises `ValueError`, because `allow_all` makes those grants have no effect.

```python
principal = Principal(id="agent:writer", roles=frozenset({"researcher"}))
```

**`MemoryPolicy`** has one `AccessRule` field for each operation family:
`read`, `write`, `search`, `traverse_up`, and `traverse_down`:

```python
researchers = AccessRule.only(roles={"researcher"}, owner=True)
policy = MemoryPolicy(
    read=researchers,
    write=AccessRule.owner(),
    search=researchers,
    traverse_up=researchers,
    traverse_down=researchers,
)
```

- **A field that you leave out is `AccessRule.owner()`.** `MemoryPolicy()` equals `MemoryPolicy.owner_private()`. `MemoryPolicy(read=AccessRule.allow())` lets everyone read, but `write`, `search`, and traversal stay owner-only.
- **`MemoryPolicy.owner_private()`** is the default for new blocks. So only the creator passes each built-in operation.
- **`MemoryPolicy.owner_or(...)`** is the usual replacement. With it, the creator and a supervisor role both keep access.
- **`MemoryPolicy.public()`** opens each built-in operation to everyone.

## Scope and policy

```text
inside handle scope AND allowed by block policy
```

- **No inheritance.** `read` on a parent does not give `read` on its descendants.
- **A handle attaches an explicit policy to a new block only with `allow_policy_override=True`.** So ordinary agents cannot make their writes public.
- **Ownership is the one exception, and it goes through `write`.** A new child already needs `write` and `traverse_down` on the parent. A delete of a block removes everything that it owns, and it does not check the own policies of the descendants.
- **So `write` on a block is authority over the existence of its subtree, not over its contents.** Descendants still decide who reads, searches, and traverses them, but they cannot outlive their owner.
- **Structure membership also goes through `write` on the container.** A bind, append, replace, or remove of a member checks the `write` rule of the container block. The facade also reads the container, so the container needs `read` too. The member only needs `read`, and its own policy does not change.
- **To outlive its owner, a block stays outside the subtree.** A link or a mount connects it, not ownership.

## Delegating to sub-agents

An orchestrator often gives sub-agents handles under its own root, and it
expects to read what they wrote. Policies do not inherit, so this needs **2
grants**. With the defaults only, it fails in both directions.

**Downward: the sub-agent has to reach the root.** `get_handle` checks that the
*new* principal can read the root. A write under the root also needs `write` and
`traverse_down`. So the root has a policy that names the sub-agents:

```python
task_root = memory.put(
    type="task",
    value={"goal": "draft the proposal"},
    policy=MemoryPolicy.owner_or(principals={"agent:step1", "agent:step2"}),
)
step1 = memory.get_handle(root=task_root, principal="agent:step1")
```

**Upward: the orchestrator has to reach the results.** `put` records the acting
principal in `created_by`. So under `MemoryPolicy.owner_private()`, only the
sub-agent can reach its own block. Handle writes get their policy from
`Memory.default_policy`, so the grant goes there:

```python
memory = Memory(
    backend=backend,
    principal=Principal(id="agent:planner", roles=frozenset({"orchestrator"})),
    default_policy=MemoryPolicy.owner_or(roles={"orchestrator"}),
)
```

Each sub-agent write now allows its creator and the orchestrator role, and
`created_by` still records which sub-agent made it.

- **A role works better than a list of principal ids.** Then one default serves each run, and no task needs a new `Memory`. A new `Memory` for each task mixes the acting principal, the hooks, and the codecs into a decision that is only about defaults. It also puts trusted setup in the path of each task.
- **`allow_policy_override=True` does not help here.** It gives the orchestrator nothing. It lets the sub-agent choose any policy, also a public one.
- **A fixed default cannot change from block to block.** When the grant depends on the block or on the run, a `before_put` hook sets the policy. See [hooks](../hooks/README.md#writing-a-hook).

## Custom operations

```python
engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
engine.require("export", block, principal)
```

- **Block rules win over engine rules.** A rule in `MemoryPolicy.operations` on the block overrides the engine default.
- **Built-in names are reserved.** A custom name cannot replace one, and an attempt raises `ValueError`.
- **An unknown operation raises `ValueError`.**

## Security guidance

- **Trusted parts:** `Memory`, backends, policy engines, hooks, and codecs.
- **What agents get:** rooted handles, or tools for one purpose.
- **Explicit principals** suit shared systems.
- **Generated block ids** are safer for untrusted input. A handle refuses a caller-provided `id`, unless it was created with `allow_custom_ids=True`.
- **A recursive delete** needs `write` on the target block only. It stops with `MemoryConflict` if the checked subtree changes.
- **A change of a block in place** with `set` or `update` needs `read` and `write`, because both return the stored block. A write-only inbox uses `put`: a new block under a container needs `write` and `traverse_down` on the container, not `read`.
- **Links that a read returns** need `read` on both endpoints. `update_link` returns the stored link, so it needs `read` and `write` on the source and `read` on the target, if the target still exists. `add_link` and `add_mount` need `write` on the source and `read` on the target.

### A missing grant fails in two ways, and only one is loud

- **`get` on a known id raises `AccessDenied`.**
- **`children`, `ancestors`, and `search` stay quiet.** They leave out the blocks that a principal cannot read, and they do not raise. So the caller gets a short list, not a refusal.
- **That is the symptom to look for.** When an orchestrator cannot see what its sub-agents wrote, the results look empty, not forbidden.
- **Refusals get to `on_denied`, but this filter does not,** because it is normal visibility and not a refusal.

### Missing versus forbidden

- **`Memory.get` returns `None`** when a block does not exist. It raises `AccessDenied` when the block exists but the principal cannot read it.
- **An application that hides whether a block exists** maps both cases to one external response.
- **Raw backend access skips policy checks.** Genie is not a process sandbox or a Python sandbox.

Related: [Memory](../memory/README.md), [Queries](../query/README.md), and
[Structures](../structures/README.md).
