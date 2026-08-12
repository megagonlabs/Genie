# Hooks

`genie.hooks` lets trusted application code observe or transform public memory
operations. Typical uses are auditing, metrics, tracing, redaction, and index
updates.

## Hook points

| Before | After | Operation |
| --- | --- | --- |
| `before_put` | `after_put` | Block creation |
| `before_get` | `after_get` | Block lookup |
| `before_search` | `after_search` | Search |
| `before_delete` | `after_delete` | Block deletion |

Hooks run in registration order. Transforming hooks pass their returned block,
query, or results to the next hook.

```python
from genie import Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_put(self, block):
        print(f"created {block.id}")


memory = Memory(hooks=[AuditHook()])
memory.hooks.register(AuditHook())
```

## Authorization

Hooks cannot widen normal access: `before_put` cannot change the authorized
parent, modified search queries are authorized again, and final search results
are filtered again. Hooks are trusted extensions with access to application
data; do not expose their configuration to untrusted agents.

## Failure and deletion

Dispatch is synchronous, exceptions propagate, and Genie does not retry hooks
or include external effects in a transaction. An `after_*` failure may occur
after the backend commit. Keep hooks fast and reliable; record an event for
slow or failure-prone asynchronous work.

Recursive deletion calls `before_delete` for every block in the authorized
snapshot. A concurrent change then raises `MemoryConflict`; before hooks may
already have run, but no `after_delete` hooks run. After hooks run only after a
successful deletion. Make before hooks repeat-safe and account for subtree
volume.

Related: [Memory](../memory/README.md), [Queries](../query/README.md), and
[Policies](../policy/README.md).
