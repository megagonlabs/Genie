# Hooks

The `genie.hooks` package lets trusted application code observe or customize
public memory operations without changing the core memory model.

Common uses include audit records, metrics, tracing, redaction, and external
index updates.

## Hook points

`MemoryHook` provides no-op methods that applications may override:

| Before | After | Operation |
| --- | --- | --- |
| `before_put` | `after_put` | Block creation |
| `before_get` | `after_get` | Block lookup |
| `before_search` | `after_search` | Search |
| `before_delete` | `after_delete` | Block deletion |

Hooks are called in registration order. Transforming hooks pass their returned
block, query, or result list to the next hook.

```python
from genie import Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(hooks=[AuditHook()])
```

Hooks can also be registered later:

```python
memory.hooks.register(AuditHook())
```

## Authorization boundaries

Hooks run inside the public `Memory` operation flow, but they cannot widen
normal authorization:

- `before_put` cannot change the already-authorized parent id;
- a query returned by `before_search` is normalized and authorized again; and
- results returned by `after_search` are filtered by scope and policy again.

Hooks are trusted extensions. They receive application data and should not be
made directly configurable by untrusted agents.

## Failure and performance

Hook dispatch is synchronous and exceptions propagate to the caller. Genie
does not retry hooks or provide a transaction that includes an external side
effect.

In particular, an `after_*` hook runs after the corresponding backend action.
If that hook fails, the backend change may already be committed. Keep hooks
fast and reliable. For slow or failure-prone integrations, record an event and
process it asynchronously in application code.

Recursive deletion calls delete hooks for every removed block. Account for
that volume when emitting audit or tracing events.

## Related guides

- [Memory operation flow](../memory/README.md)
- [Query authorization](../query/README.md)
- [Policies](../policy/README.md)
