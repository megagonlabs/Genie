# Policies

`genie.policy` provides access rules for single blocks. A request must pass
both the handle's scope and the block's `MemoryPolicy`.

## Principals and rules

`Principal` identifies a user, an agent, or a service by `id`, `roles`, and
optional `metadata`. An `AccessRule` says who passes one operation:
`AccessRule.allow()` for everyone, `AccessRule.deny()` for nobody,
`AccessRule.owner()` for the block creator, and `AccessRule.only(...)` for
chosen `principals`, chosen `roles`, or those plus `owner=True`.

```python
principal = Principal(id="agent:writer", roles=frozenset({"researcher"}))
```

`Principal` ids and role names must be non-empty strings. Invalid principal
input raises an error. It never falls back to the default `system` identity.
Policy rules check the types of their fields and copy any mutable input sets.

`MemoryPolicy` has one `AccessRule` field per operation family: `read`,
`write`, `search`, `traverse_up`, and `traverse_down`:

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

New blocks use `MemoryPolicy.owner_private()` by default, so only the creator
passes every built-in operation. An application may replace that default. A
common choice is `MemoryPolicy.owner_or(...)`, so that creators and a supervisor
role keep access. Use `MemoryPolicy.public()` only when every built-in operation
should be open to everyone.

## Scope and policy

```text
inside handle scope AND allowed by block policy
```

`read` on a parent does not grant `read` on its descendants. Genie does not
implement policy inheritance. A handle needs `allow_policy_override=True` before
it can attach an explicit policy to a new block. This stops ordinary agents from
making their writes public.

Ownership is the one exception, and it runs through `write`. Creating a child
already needs `write` on the parent, and deleting a block removes everything it
owns without consulting the descendants' own policies. So `write` on a block is
authority over the existence of its subtree, not over its contents. Descendants
still decide who reads, searches, and traverses them, but they cannot outlive
their owner. A block that must survive its owner belongs outside the subtree,
reached through a link or a mount instead of being owned.

## Delegating to sub-agents

An orchestrator often gives sub-agents handles under its own root and expects to
read what they wrote. Policies do not inherit, so this needs two grants. With
the defaults alone it fails in both directions.

Downward, the sub-agent has to reach the root. `get_handle` checks that the
*new* principal can read the root, and writing under it also needs `write` and
`traverse_down`. So the root carries a policy that names the sub-agents:

```python
task_root = memory.put(
    type="task",
    value={"goal": "draft the proposal"},
    policy=MemoryPolicy.owner_or(principals={"agent:step1", "agent:step2"}),
)
step1 = memory.get_handle(root=task_root, principal="agent:step1")
```

Upward, the orchestrator has to reach the results. `put` records the acting
principal in `created_by`, so under `MemoryPolicy.owner_private()` a
sub-agent's block is reachable only by that sub-agent. Handle writes take their
policy from `Memory.default_policy`, so set it there:

```python
memory = Memory(
    backend=backend,
    principal=Principal(id="agent:planner", roles=frozenset({"orchestrator"})),
    default_policy=MemoryPolicy.owner_or(roles={"orchestrator"}),
)
```

Every sub-agent write now allows its creator and the orchestrator role, and
`created_by` still records which sub-agent produced it.

Prefer a role over a list of principal ids. One default then serves every run,
and you do not have to build a new `Memory` for each task. Building one per task
mixes the acting principal, the hooks, and the codecs into a decision that is
only about defaults, and it puts trusted setup in the path of every task.

Do not use `allow_policy_override=True` on the sub-agent handle for this. It
gives the orchestrator nothing. It lets the sub-agent choose any policy,
including a public one.

A fixed default cannot vary by block. When the grant depends on the block or on
the run, set the policy from a `before_put` hook instead. See
[hooks](../hooks/README.md#writing-a-hook).

## Custom operations

An application may add its own operations, such as `export` or `approve`:

```python
engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
engine.require("export", block, principal)
```

A rule in `MemoryPolicy.operations` on the block overrides the engine default.
Custom names cannot replace the built-in ones, and an unknown operation raises
`ValueError`.

## Security guidance

- Keep `Memory`, backends, policy engines, hooks, and codecs trusted.
- Give agents rooted handles, or tools built for one purpose.
- Use explicit principals in shared systems.
- Prefer generated block ids for untrusted input.
- Recursive deletion needs `write` on the target block only, and it stops if
  the checked subtree changes.
- A link result needs `read` on both endpoints.

A missing grant fails in two different ways, and only one of them is loud.
`get` on a known id raises `AccessDenied`. `children`, `ancestors`, and `search`
leave out the blocks a principal cannot read and do not raise, so the caller
gets a short list instead of a refusal. When an orchestrator cannot see what its
sub-agents wrote, that is the symptom: the results look empty rather than
forbidden. Refusals reach `on_denied`; this filtering does not, because it is
normal visibility and not a refusal.

`Memory.get` returns `None` when a block is missing, and raises `AccessDenied`
when the block exists but cannot be read. An application that wants to hide
whether a block exists should map both cases to one external response. Raw
backend access skips policy checks. Genie is not a process sandbox or a Python
sandbox.

Related: [Memory](../memory/README.md), [Queries](../query/README.md), and
[Structures](../structures/README.md).
