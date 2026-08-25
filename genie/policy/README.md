# Policies

`genie.policy` provides access rules for single blocks. A request must pass both
the handle's scope and the block's policy.

## Principals and rules

`Principal` identifies a user, an agent, or a service by id, roles, and optional
metadata. An `AccessRule` can allow everyone, the creator of the block, chosen
ids, chosen roles, or a combination of these.

```python
principal = Principal(id="agent:writer", roles=frozenset({"researcher"}))
```

Principal ids and role names must be non-empty strings. Invalid principal input
raises an error. It never falls back to the default `system` identity. Policy
rules check the types of their fields and copy any mutable input sets.

`MemoryPolicy` controls read, write, search, upward traversal, and downward
traversal:

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

New blocks use `MemoryPolicy.owner_private()` by default. An application may
replace that default. A common choice is `owner_or(...)`, so that creators and a
supervisor role keep access. Use `MemoryPolicy.public()` only when every
built-in operation should be open to everyone.

## Scope and policy

```text
inside handle scope AND allowed by block policy
```

Access to a parent does not grant access to its descendants. Genie does not
implement policy inheritance. A handle needs `allow_policy_override=True` before
it can attach an explicit policy to a new block. This stops ordinary agents from
making their writes public.

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
- Recursive deletion needs write access to every affected block, and it stops if
  the checked subtree changes.
- A link result needs both endpoints to be readable.

`Memory.get` returns `None` when a block is missing, and raises `AccessDenied`
when the block exists but cannot be read. An application that wants to hide
whether a block exists should map both cases to one external response. Raw
backend access skips policy checks. Genie is not a process sandbox or a Python
sandbox.

Related: [Memory](../memory/README.md), [Queries](../query/README.md), and
[Structures](../structures/README.md).
