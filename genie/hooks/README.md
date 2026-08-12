# Hooks

`genie.hooks` lets trusted application code observe or transform logical memory
operations. Common uses are auditing, metrics, tracing, redaction, and index
maintenance.

## Coverage

| Before | After | Operations |
| --- | --- | --- |
| `before_put` | `after_put` | Block creation |
| `before_update` | `after_update` | Block `set` and dictionary/metadata `update` |
| `before_get` | `after_get` | Exact block lookup |
| `before_search` | `after_search` | Search |
| `before_delete` | `after_delete` | Each block in recursive deletion |
| `before_add_link` | `after_add_link` | Regular, acyclic, and mount link creation |
| `before_update_link` | `after_update_link` | Link metadata update |
| `before_structure_change` | `after_structure_change` | Keys, log appends, and temporal membership |

Structure events use immutable `StructureChange` values. `kind` distinguishes
keyed collections, event logs, and temporal collections; `action` identifies
the specific mutation. Versioned records reuse key and log events rather than
emitting duplicates.

Traversal, link listing, facade reads, handle creation, and direct backend
calls do not have separate hooks. Facades may perform ordinary `get` calls
while resolving blocks, so applications should treat `get` hooks as block
lookups rather than one event per high-level facade read. Backend access is a
trusted boundary and intentionally bypasses hooks and policy.

## Writing a hook

Hooks run in registration order. Transforming callbacks pass their result to
the next hook.

```python
from genie import HookContext, Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_update(self, context: HookContext, previous, stored):
        print(context.operation_id, context.operation, stored.id)


audit_hook = AuditHook()
memory = Memory(hooks=[audit_hook])
```

Every callback receives a `HookContext`. It provides one operation id shared by
before and after callbacks, an optional parent operation id, the operation type,
acting principal id, zero-based depth, and an origin of `caller` or `hook`.
`Memory` and structure facades create and pass this context. Code that dispatches
through `HookRegistry` directly must use the context yielded by
`HookRegistry.operation()`; dispatch does not create an implicit operation.

Block-update hooks may transform only value, metadata, and content type. Link
hooks may transform only metadata. Genie checks protected identity, ownership,
topology, policy, timestamp, and version fields before persistence. Structural
hooks observe immutable backend-owned records rather than rewriting them.

## Nested operations and recursion

A hook may call memory again. `HookRegistry` tracks active callback pairs with
`ContextVar`; when the same callback is reached recursively, only that callback
is skipped. Other hooks and other callbacks still run.

```text
caller PUT
  -> AuditHook.after_put
       -> PUT audit block
            -> AuditHook.after_put skipped
            -> other callbacks still run
```

Each registry also limits hook-originated nesting, defaulting to depth 16.
Exceeding it raises `HookRecursionError`. Context is isolated between threads
and asynchronous tasks and is reset even when a callback raises.

A hook that wants only caller-initiated events can return early when
`context.origin == "hook"`. This also prevents different hooks that create
derived records from amplifying one another.

Trusted migrations or hook-internal code may suppress selected hooks or the
whole registry in the current context:

```python
with memory.hooks.suppress(audit_hook):
    memory.put(type="audit", value={"event": "bootstrap"})

with memory.hooks.suppress():
    memory.put(type="internal", value="no hooks")
```

Suppression is intentionally available on the trusted registry, not on
`MemoryHandle` operations.

## Authorization and failures

Hooks cannot widen normal access: `before_put` cannot change the authorized
parent, changed searches are authorized again, and final search results are
filtered again. Hooks are trusted extensions with application data access.

Dispatch is synchronous, exceptions propagate, and Genie does not retry hooks
or include external effects in backend transactions. A before-hook failure
prevents its mutation; an after-hook failure occurs after commit. Keep hooks
fast, repeat-safe, and reliable.

Recursive deletion calls `before_delete` for every block in the authorized
snapshot. A concurrent change raises `MemoryConflict`; before hooks may already
have run, but after hooks run only following successful deletion. Cascading
backend cleanup of links and structural references is represented by the block
deletion rather than separate structural events.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
[Queries](../query/README.md), and [Policies](../policy/README.md).
