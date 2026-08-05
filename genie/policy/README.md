# Policies

The `genie.policy` package provides block-level access rules. Policies work
with `MemoryHandle` scope; neither replaces the other.

## Principals and rules

`Principal` identifies the user, agent, or service performing an operation:

```python
principal = Principal(
    id="agent:writer",
    roles=frozenset({"researcher"}),
)
```

An `AccessRule` may allow everyone, the block creator, selected principal ids,
selected roles, or a combination of those choices.

## Memory policies

`MemoryPolicy` controls five built-in operations:

- read;
- write;
- search;
- traverse up; and
- traverse down.

```python
policy = MemoryPolicy(
    read=AccessRule.only(roles={"researcher"}, owner=True),
    write=AccessRule.owner(),
    search=AccessRule.only(roles={"researcher"}, owner=True),
    traverse_up=AccessRule.only(roles={"researcher"}, owner=True),
    traverse_down=AccessRule.only(roles={"researcher"}, owner=True),
)
```

New blocks use `MemoryPolicy.owner_private()` by default. Applications may set
another default:

```python
memory = Memory(
    principal="service:memory",
    default_policy=MemoryPolicy.owner_or(roles={"memory_admin"}),
)
```

`owner_or(...)` is useful when creators keep access while a supervisor or
service role also manages their memory. Use `MemoryPolicy.public()` only when
all built-in operations should be open.

## Scope and policy

A request succeeds only when both checks pass:

```text
inside handle scope AND allowed by block policy
```

Handle scope controls where a caller can operate. Block policy controls what
that principal may do with each block. Access to a parent does not grant access
to descendants; policy inheritance is not implemented.

Handles cannot attach an explicit policy to new blocks unless created with
`allow_policy_override=True`. This prevents an ordinary agent from making its
own writes public.

## Custom operations

Applications can define operations such as `export` or `approve`:

```python
engine = PolicyEngine(
    operations={"export": AccessRule.only(roles={"compliance"})},
)
engine.require("export", block, principal)
```

A block-level `MemoryPolicy.operations` rule overrides the engine default for
the same custom operation. Custom names cannot replace built-in operations.
Unknown operations raise `ValueError` rather than silently allowing access.

## Security guidance

- Keep the full `Memory`, backend, policy engine, hooks, and codecs in trusted
  application code.
- Give agents rooted handles or application-defined tools with the minimum
  operations they need.
- Use explicit principals in shared systems.
- Prefer generated internal block ids for untrusted input.
- Recursive deletion requires write access to every affected block.
- Link results are visible only when both endpoints are readable.

`Memory.get` returns `None` for a missing block and raises `AccessDenied` for a
known but unreadable block. Applications that must hide existence should map
both outcomes to the same external response.

Genie enforces access rules inside its API, but it is not a process or Python
sandbox. Code that holds a raw backend can bypass the public policy layer.

## Related guides

- [Memory and handles](../memory/README.md)
- [Queries](../query/README.md)
- [Structures](../structures/README.md)
