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
| `before_traverse` | `after_traverse` | Children, ancestors, link and mount listing, and containment |
| `before_delete` | `after_delete` | Each block in recursive deletion |
| `before_add_link` | `after_add_link` | Regular, acyclic, and mount link creation |
| `before_update_link` | `after_update_link` | Link metadata update |
| `before_structure_change` | `after_structure_change` | Keys, log appends, and temporal membership |

Two unpaired callbacks cover every operation: `on_denied` reports refusals and
`on_operation_end` reports outcomes.

Structure events use immutable `StructureChange` values. `kind` distinguishes
keyed collections, event logs, and temporal collections; `action` identifies
the specific mutation. Versioned records reuse key and log events rather than
emitting duplicates.

Mount resolution, facade reads, handle creation, and direct backend calls have
no paired callbacks. Mount resolution and handle creation still report refusals;
see below. Facades may perform ordinary `get` calls while resolving blocks, so
applications should treat `get` hooks as block lookups rather than one event per
high-level facade read. Backend access is a trusted boundary and intentionally
bypasses hooks and policy.

## Traversal

A read that follows relationships names a starting block rather than the blocks
it returns, so one call can disclose many. `before_traverse` and
`after_traverse` cover `children`, `ancestors`, `list_links`, `contains`,
`list_mounts`, and `discover_mounts`. Both receive an immutable `Traversal`: the
before callback sees the read that is about to run, and the after callback sees
the same read carrying what it disclosed.

| `kind` | `origin_id` | Disclosed by the after callback |
| --- | --- | --- |
| `children` | Parent block | `blocks` |
| `ancestors` | Starting block | `blocks` |
| `links` | Listed block | `links`, with the requested `relation` and `direction` |
| `contains` | Subtree root | `contained`, for the candidate in `target_id` |
| `mounts` | Source block | `links`, the mounts leaving that block |
| `mounts.subtree` | Scanned root | `links`, the mounts found anywhere below it |

`mounts` and `mounts.subtree` report the same kind of record from very different
amounts of memory, so they are separate kinds: a scan is one event covering every
readable block under `origin_id`, and a hook that treated it as a single-block
listing would understate what it disclosed. Both report the reserved mount
relation and the outgoing `direction` they filtered on.

```python
class ReadVolumeHook(MemoryHook):
    def after_traverse(self, context, traversal):
        record(
            principal=context.principal_id,
            kind=traversal.kind,
            origin=traversal.origin_id,
            disclosed=len(traversal.blocks) + len(traversal.links),
        )
```

Traversal results are backend-owned and already filtered by the reading
principal's read policy, so these callbacks observe a read rather than narrowing
or widening it. A block the principal cannot read is absent from `blocks`, and
`contains` reports `contained=False` instead of refusing.

Structures walk the ownership tree through these operations rather than around
them, so `OwnershipTree.descendants()` reports one `children` traversal for each
block it visits. A subtree read is therefore visible in the trail as the reads
that produced it.

The event reports the read `Memory` performed. A `MemoryHandle` hides links
whose other endpoint lies outside its root after that read completes, so a
handle's link listing can report links the caller never receives. The listing
is never narrower than what the caller sees.

## Terminal events

Paired callbacks report work that happens; `on_operation_end` reports what
became of it. It runs exactly once for every operation that starts, after that
operation's other callbacks, with one of three outcomes.

| Outcome | Meaning |
| --- | --- |
| `success` | The operation completed and any mutation committed. |
| `denied` | Policy refused the operation. |
| `error` | The operation failed for any other reason. |

Because it always runs, it is where a hook closes a span, records latency, and
releases state stashed by a before callback. It is also the only callback that
observes refusals and failures: a denied `set` never reaches `before_update`,
and a failed one reaches `before_update` but never `after_update`.

```python
class MetricsHook(MemoryHook):
    def before_put(self, context, block):
        self.started[context.operation_id] = monotonic()
        return block

    def on_operation_end(self, context, outcome, error):
        started = self.started.pop(context.operation_id, None)
        record(context.operation, outcome, started)
```

A terminal event identifies the operation and principal rather than the target
block. Use `on_denied` for the subject of a refusal, and the paired callbacks
for the subject of successful work.

An operation rejected before it starts has no terminal event, so exceeding
`max_depth` raises `HookRecursionError` without one.

## Refusals

`on_denied` reports every refusal Genie raises, with the block it applies to
and the check that produced it. It is an observation: the refusal stands, and
a hook cannot reverse it.

```python
class SecurityHook(MemoryHook):
    def on_denied(self, context, operation, subject_id, reason):
        alert(context.principal_id, context.operation, operation, subject_id)
```

The `operation` argument names what refused. Policy operations are bare `read`,
`write`, `search`, `traverse_up`, and `traverse_down` values. `MemoryHandle`
boundaries are namespaced `HandleCapability` values, so the two never collide
and an attempt to leave a sandbox is never confused with an ordinary
permission failure.

| Capability | Refused because |
| --- | --- |
| `handle.subtree` | The block is outside the handle root. |
| `handle.ancestors` | Ancestor traversal or search is disabled. |
| `handle.global_search` | Global search is disabled. |
| `handle.mounts` | Mount access is disabled. |
| `handle.policy_override` | An explicit block policy was passed. |
| `handle.root_required` | An unbounded handle has no root to infer. |
| `handle.principal` | A child handle tried to change principal. |

Coverage is complete for the security boundary. Policy refusals are reported
for block operations, search, links, mounts, traversal, listing, `contains`,
and handle creation. Handle refusals are reported for every boundary and
capability check, including child handles that try to widen access. Each
refusal is paired with a `denied` terminal event that shares its operation id.

Ordinary visibility filtering is not a refusal. Search results and traversal
results that a principal cannot read are omitted without reporting anything,
so `on_denied` stays a signal rather than a stream.

Refusals raised inside an `OwnershipTree` facade root are not reported. That
bound is a view over an access object the caller already holds, so leaving it
cannot widen access; the underlying handle and policy still refuse.

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
acting principal id, zero-based depth, an origin of `caller` or `hook`,
inherited `attributes`, and the `started_at` timestamp of the operation.
`Memory` and structure facades create and pass this context. Code that dispatches
through `HookRegistry` directly must use the context yielded by
`HookRegistry.operation()`; dispatch does not create an implicit operation.

Block-update hooks may transform only value, metadata, and content type. Link
hooks may transform only metadata. Genie checks protected identity, ownership,
topology, policy, timestamp, and version fields before persistence. Structural
hooks observe immutable backend-owned records rather than rewriting them.

## Scopes and attributes

Genie names the operations it performs. Applications name the work that causes
them, such as an agent run, a turn, or a tool call. `HookRegistry.scope()`
groups the operations of one caller-defined span and labels them:

```python
with memory.hooks.scope("agent.turn", attributes={"run_id": run, "turn": "7"}):
    handle.put(type="note", value=draft)
    handle.search("open questions")
```

A scope dispatches no callbacks. It opens causal context, so operations started
inside it become its children and inherit its attributes at any nesting depth.
A hook can therefore group work by label without knowing how the calls nest:

```python
class AuditHook(MemoryHook):
    def on_operation_end(self, context, outcome, error):
        record(
            run=context.attributes.get("run_id"),
            operation=context.operation,
            outcome=outcome,
            elapsed=datetime.now(UTC) - context.started_at,
        )
```

Attributes are a read-only mapping of strings to strings. Each context inherits
the attributes of the operation or scope containing it, and attributes added by
the operation itself win on conflict. `Memory` adds the attributes it was
configured with:

```python
memory = Memory(hooks=[audit_hook], attributes={"service": "memory"})
```

A `MemoryHandle` adds its own boundary on top of them:

| Attribute | Value |
| --- | --- |
| `handle.id` | Stable id of the handle that performed the operation. |
| `handle.root` | Handle root. Omitted by an unbounded handle. |
| `handle.capabilities` | Enabled `ancestors`, `global_search`, `mounts`, and `policy_override` capabilities, comma-separated. |

Two handles with the same principal are therefore distinguishable in the trail,
including in the refusal reported when one tries to leave its sandbox.

Attributes describe the causal chain rather than the acting object alone: a
derived write a hook performs inside a handle operation inherits that handle's
labels. Use `origin` and `depth` to tell caller-initiated work from work a hook
caused.

Scope contexts stay outside the callback contract. A scope reports the
`HookOperation.SCOPE` operation and carries its free-form name in `scope_name`;
every operation Genie performs reports its own `HookOperation` and a
`scope_name` of `None`. So `context.operation` is always a strongly typed
`HookOperation`, and a context is either a named span or performed work, never
both.

Hooks never receive a scope context. `HookRegistry.operation()` refuses
`HookOperation.SCOPE`, and dispatching a callback with a scope context raises
`ValueError`.

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
and asynchronous tasks and is reset even when a callback raises, so concurrent
agent runs never observe one another's scopes.

A scope is a nesting level as well: operations directly inside one report depth
1, and a scope opened from inside a hook is bounded by `max_depth` exactly like
an operation.

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

Dispatch is synchronous, and Genie does not retry hooks or include external
effects in backend transactions. Keep hooks fast, repeat-safe, and reliable.
What a failing hook costs depends on whether it can still change the outcome.

| Callbacks | A failure |
| --- | --- |
| `before_*` and `after_search` | Stops dispatch and fails the operation. |
| `after_*` returning `None`, `on_denied`, `on_operation_end` | Is collected; every hook still runs. |

A failure before the commit point is a veto and has to stop everything: an
aborted `put` stores nothing, and a failed `after_search` returns no results
rather than results a redaction hook never reviewed. The observing callbacks
report work that is already decided. Stopping there cannot undo it and can only
destroy the records the remaining hooks and blocks are owed, so dispatch
continues and the collected failures are reported once it finishes: attached as
notes to an exception the caller is already receiving, and raised otherwise,
first failure first, with the rest as notes on it so none hides another. A
failing `on_denied` therefore still lets the `AccessDenied` through, and a
failing `on_operation_end` still lets the operation's own error through.

A best-effort sink can declare that its own outages must not reach callers:

```python
class AuditHook(MemoryHook):
    fail_open = True

    def after_put(self, context, block):
        self.sink.send(context.operation_id, block.id)
```

`fail_open` covers the observing callbacks only, for the same reason they are
isolated. `before_*` and `after_search` ignore it, because containing a failure
there would silently store or return the value the hook existed to change.
Contained failures are logged with their traceback to the
`genie.hooks.registry` logger, so a sink that is failing stays visible.

Recursive deletion calls `before_delete` for every block in the authorized
snapshot. A concurrent change raises `MemoryConflict`; before hooks may already
have run, but after hooks run only following successful deletion. Every deleted
block then reaches every hook before any failure surfaces, so deleting five
blocks with a sink that fails on the third still records five deletions.
Cascading backend cleanup of links and structural references is represented by
the block deletion rather than separate structural events.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
[Queries](../query/README.md), and [Policies](../policy/README.md).
