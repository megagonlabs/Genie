# Policies

`genie.policy` provides block-level access rules. A request must pass both the
handle's scope and the block's policy.

## Principals and rules

`Principal` identifies a user, agent, or service by id, roles, and optional
metadata. `AccessRule` can allow everyone, the block creator, selected ids,
selected roles, or a combination.

```python
principal = Principal(id="agent:writer", roles=frozenset({"researcher"}))
```

Principal ids and role names must be non-empty strings. Invalid principal
inputs raise instead of falling back to the default `system` identity. Policy
rules validate their field types and copy mutable input sets.

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

New blocks default to `MemoryPolicy.owner_private()`. Applications may replace
the default, commonly with `owner_or(...)` so creators and a supervisor role
retain access. Use `MemoryPolicy.public()` only when every built-in operation
should be open.

## Scope and policy

```text
inside handle scope AND allowed by block policy
```

Parent access does not grant descendant access; Genie does not implement
policy inheritance. Handles need `allow_policy_override=True` to attach an
explicit policy to new blocks, preventing ordinary agents from making their
writes public.

## Custom operations

Applications may add operations such as `export` or `approve`:

```python
engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
engine.require("export", block, principal)
```

A block-level `MemoryPolicy.operations` rule overrides the engine default.
Custom names cannot replace built-ins; unknown operations raise `ValueError`.

## Security guidance

- Keep `Memory`, backends, policy engines, hooks, and codecs trusted.
- Give agents rooted handles or minimum-purpose tools.
- Use explicit principals in shared systems.
- Prefer generated block ids for untrusted input.
- Recursive deletion requires write access to every affected block and aborts
  if the checked subtree changes.
- Link results require both endpoints to be readable.

`Memory.get` returns `None` when missing and raises `AccessDenied` when known
but unreadable. Applications that hide existence should map both to one
external response. Raw backend access bypasses policy checks; Genie is not a
process or Python sandbox.

Related: [Memory](../memory/README.md), [Queries](../query/README.md), and
[Structures](../structures/README.md).
