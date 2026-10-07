# Hooks

`genie.hooks` lets trusted application code watch or change logical memory
operations. Use it for auditing, metrics, tracing, redaction, and keeping
indexes up to date.

## Coverage

| Before | After | Operations |
| --- | --- | --- |
| `before_put` | `after_put` | Block creation |
| `before_update` | `after_update` | Block `set` and dictionary/metadata `update` |
| `before_get` | `after_get` | Exact block lookup |
| `before_search` | `after_search` | Search |
| `before_traverse` | `after_traverse` | Children, ancestors, link and mount listing, and containment |
| `before_delete` | `after_delete` | Each block in a recursive deletion |
| `before_add_link` | `after_add_link` | Regular, acyclic, and mount link creation |
| `before_update_link` | `after_update_link` | Link metadata update |
| `before_structure_change` | `after_structure_change` | Keys, log appends, and temporal membership |

Three callbacks have no before/after pair, and none of them is tied to one kind
of operation:

- **`on_operation_end`** runs once for every operation that starts, including ones with no pair in the table above, such as mount resolution and handle creation.
- **`on_denied`** runs whenever a request is refused.
- **`on_suppressed`** runs when dispatch is turned off.

Other notes:

- **Structure events use immutable `StructureChange` values.** `kind` tells keyed collections, event logs, and temporal collections apart, and `action` names the exact change. Versioned records reuse the key and log events instead of sending duplicates.
- **No paired callbacks** for mount resolution, facade reads, handle creation, and direct backend calls. Mount resolution and handle creation still report refusals.
- **`get` hooks are block lookups, not facade reads.** A facade may run ordinary `get` calls while it resolves blocks, so one high-level facade read is not one event.
- **Backend access is a trusted boundary,** and it skips hooks and policy on purpose.

## Traversal

A read that follows relationships names a starting block rather than the blocks
it returns, so one call can disclose many blocks.

- **Covered calls:** `children`, `ancestors`, `list_links`, `contains`, `list_mounts`, and `discover_mounts`.
- **Both callbacks receive an immutable `Traversal`.** The before callback sees the read that is about to run. The after callback sees the same read, now carrying what it disclosed.

| `kind` | `origin_id` | Disclosed by the after callback |
| --- | --- | --- |
| `children` | Parent block | `blocks` |
| `ancestors` | Starting block | `blocks` |
| `links` | Listed block | `links`, with the requested `relation` and `direction` |
| `contains` | Subtree root | `contained`, for the candidate in `target_id` |
| `mounts` | Source block | `links`, the mounts leaving that block |
| `mounts.subtree` | Scanned root | `links`, the mounts found anywhere below it |

- **`mounts` and `mounts.subtree` are separate kinds on purpose.** They return the same kind of record, but they read very different amounts of memory. A scan is one event that covers every readable block under `origin_id`, and a hook that treated it as a single-block listing would report far less than it disclosed. Both report the reserved mount relation and the outgoing `direction` they filtered on.

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

- **These callbacks observe a read. They do not narrow it or widen it.** Traversal results belong to the backend and are already filtered by the `read` rule of the reading principal. A block the principal cannot `read` is missing from `blocks`, and `contains` reports `contained=False` instead of refusing.
- **Structures walk the ownership tree through these operations, not around them.** So `OwnershipTree.descendants()` reports one `children` traversal for each block it visits, and a subtree read appears in the trail as the reads that produced it.
- **The event reports the read that `Memory` performed.** A `MemoryHandle` hides links whose other endpoint is outside its root, and it does that after the read finishes. So a handle's link listing can report links that the caller never receives. The listing is never narrower than what the caller sees.

## Terminal events

Paired callbacks report work that happens. `on_operation_end` reports what
became of it. It runs exactly once for every operation that starts, after that
operation's other callbacks, with one of three outcomes.

| Outcome | Meaning |
| --- | --- |
| `success` | The operation finished, and any change was committed. |
| `denied` | Policy refused the operation. |
| `error` | The operation failed for some other reason. |

- **Because it always runs,** it is the right place to close a tracing span (one timed unit of work in a trace), record how long an operation took, and release state that a before callback saved.
- **It is the only callback that sees refusals and failures.** A denied `set` never reaches `before_update`. A failed one reaches `before_update` but never `after_update`.

```python
class MetricsHook(MemoryHook):
    def before_put(self, context, block):
        self.started[context.operation_id] = monotonic()
        return block

    def on_operation_end(self, context, outcome, error):
        started = self.started.pop(context.operation_id, None)
        record(context.operation, outcome, started)
```

- **A terminal event names the operation and the principal, not the target block.** Use `on_denied` for the subject of a refusal, and the paired callbacks for the subject of successful work.
- **An operation rejected before it starts has no terminal event.** So going past `max_depth` raises `HookRecursionError` without one.

## Refusals

`on_denied` reports every refusal that Genie raises, with the block it applies
to and the check that caused it. It is only an observation: the refusal stands,
and a hook cannot undo it.

```python
class SecurityHook(MemoryHook):
    def on_denied(self, context, operation, subject_id, reason):
        alert(context.principal_id, context.operation, operation, subject_id)
```

- **The `operation` argument names what refused.** Policy operations are the plain values `read`, `write`, `search`, `traverse_up`, and `traverse_down`. `MemoryHandle` boundaries are `HandleCapability` values with a namespace.
- **The two can never collide,** so an attempt to leave a sandbox is never mistaken for an ordinary permission failure.

| Capability | Refused because |
| --- | --- |
| `handle.subtree` | The block is outside the handle `root`. |
| `handle.ancestors` | The handle has `allow_up=False`. |
| `handle.global_search` | The handle has `allow_global=False`. |
| `handle.mounts` | The handle has `allow_mounts=False`. |
| `handle.policy_override` | An explicit `policy` was passed without `allow_policy_override=True`. |
| `handle.root_required` | An unbounded handle has no `root` to infer. |
| `handle.principal` | A child handle tried to change `principal`. |

- **Coverage of the security boundary is complete.** Policy refusals are reported for block operations, search, links, mounts, traversal, listing, `contains`, and handle creation. Handle refusals are reported for every boundary and capability check, including child handles that try to widen access. Every refusal comes with a `denied` terminal event that shares its operation id.
- **Ordinary visibility filtering is not a refusal.** Search and traversal results that a principal cannot read are left out without reporting anything, so `on_denied` stays a signal instead of becoming a stream.
- **Refusals raised inside an `OwnershipTree` facade root are not reported.** That bound is a view over an access object the caller already holds, so leaving it cannot widen access. The underlying handle and policy still refuse.

## Writing a hook

Hooks run in the order you register them. A transforming callback passes its
result to the next hook.

```python
from genie import HookContext, Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_update(self, context: HookContext, previous, stored):
        print(context.operation_id, context.operation, stored.id)


audit_hook = AuditHook()
memory = Memory(hooks=[audit_hook])
```

**Every callback receives a `HookContext`.** It carries one operation id that
the before and after callbacks share, an optional parent operation id, the
operation type, the id of the acting principal, a zero-based depth, an origin of
`caller` or `hook`, inherited `attributes`, and the `started_at` timestamp of the
operation.

- **`Memory` and the structure facades create this context** and pass it on.
- **Code that dispatches through `HookRegistry` directly** must use the context that `HookRegistry.operation()` yields, because dispatch does not create an operation by itself.

### What a hook may change

| Hook | May change |
| --- | --- |
| Block update | The value, the metadata, and the content type |
| Link | The metadata only |
| Block creation (`before_put`) | The value, the metadata, the content type, and the `policy` |
| Structural | Nothing. They observe immutable backend-owned records |

- **Genie checks the protected fields before it stores anything:** identity, ownership, topology, policy, timestamp, and version.
- **Every `MemoryBlock` field not listed above is protected,** including `id`, `type`, `parent_id`, `created_by`, `created_at`, `updated_at`, and `version`. Changing one raises `ValueError`, and nothing is stored.
- **A `before_put` policy must be a `MemoryPolicy`.** Anything else raises `TypeError`, and nothing is stored.
- **`before_put` is different from `before_update`,** because a policy is chosen when a block is created and stays stable after that. `before_put` may set the new block's `policy`; `before_update` may not.
- **`before_put` still cannot change the authorized parent.**

### `before_put` is the only per-block policy choice

- **A `MemoryHandle` refuses explicit policies** unless it was created with `allow_policy_override=True`.
- **`Memory.default_policy` is one fixed policy** for every write.
- **A hook sees the block and the `HookContext`,** so it can decide from the block type, the metadata, the acting principal, or the attributes of the surrounding scope:

```python
class GrantHook(MemoryHook):
    def before_put(self, context, block):
        orchestrator = context.attributes.get("task.orchestrator")
        if orchestrator is None:
            return block
        return block.with_updates(
            policy=MemoryPolicy.owner_or(principals={orchestrator}),
        )
```

- **A hook that sets policies is part of your access control.** Register it as mandatory, so later code cannot detach it, and keep it in trusted code.
- **Record its decisions too.** A reader of the audit trail otherwise cannot tell why a block has the policy it has.

## The registry

`HookRegistry` owns the dispatch order, and the list of installed hooks is
itself auditable. `hooks` lists them, so startup code can assert that the trail
is wired up:

```python
registry = HookRegistry()
registry.register(audit_hook, mandatory=True)
memory = Memory(hooks=registry, attributes={"service": "memory"})

assert audit_hook in memory.hooks.hooks
assert memory.hooks.mandatory == (audit_hook,)
```

- **Hooks are identified by object identity.** Two instances of the same class are two hooks.
- **Registering one instance twice raises `ValueError`** instead of quietly reporting every operation twice.
- **`unregister` removes a hook by the same identity** and raises if that hook is not installed.
- **Re-registering changes the order.** A hook registered again later is added at the end.

| Attempt | Ordinary hook | Mandatory hook |
| --- | --- | --- |
| `unregister(hook)` | Removed | `ValueError` |
| `suppress(hook)` | Silent for the window | `ValueError` |
| `suppress()` | Silent for the window | Keeps reporting |
| `memory.hooks = other` | `AttributeError` | `AttributeError` |

- **`mandatory=True` means neither unregistered nor suppressed.** That is how an audit hook outlives the code that runs after it.
- **`Memory.hooks` is read-only,** so the registry a memory dispatches through is fixed for its whole lifetime. To change which hooks run, register or unregister on that registry, which a mandatory hook survives. Replacing the whole registry is not a way around this.

## Scopes and attributes

Genie names the operations it performs. Applications name the work that causes
them, such as an agent run, a turn, or a tool call. `HookRegistry.scope()`
groups the operations of one caller-defined span and gives them a label:

```python
with memory.hooks.scope("agent.turn", attributes={"run_id": run, "turn": "7"}):
    handle.put(type="note", value=draft)
    handle.search("open questions")
```

- **A scope dispatches no callbacks.** It opens a causal context: operations started inside it become its children and inherit its attributes at any nesting depth. So a hook can group work by label without knowing how the calls nest:

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

- **Attributes are a read-only mapping of strings to strings.** Each context inherits the attributes of the operation or scope that contains it, and attributes added by the operation itself win when there is a conflict.
- **`Memory` adds the attributes it was configured with:** `Memory(hooks=[audit_hook], attributes={"service": "memory"})`.

A `MemoryHandle` adds its own boundary on top of those:

| Attribute | Value |
| --- | --- |
| `handle.id` | Stable id of the handle that performed the operation. |
| `handle.root` | Handle root. An unbounded handle leaves this out. |
| `handle.capabilities` | Enabled `ancestors`, `global_search`, `mounts`, and `policy_override` capabilities, separated by commas. |

- **So two handles with the same principal can be told apart** in the trail, including in the refusal reported when one tries to leave its sandbox.
- **Attributes describe the causal chain, not only the acting object.** A derived write that a hook performs inside a handle operation inherits that handle's labels. Use `origin` and `depth` to tell caller-initiated work apart from work that a hook caused.
- **Scope contexts stay outside the callback contract.** A scope reports the `HookOperation.SCOPE` operation and carries its free-form name in `scope_name`. Every operation Genie performs reports its own `HookOperation` and a `scope_name` of `None`. So `context.operation` is always a strongly typed `HookOperation`, and a context is either a named span or performed work, never both.
- **Hooks never receive a scope context.** `HookRegistry.operation()` refuses `HookOperation.SCOPE`, and dispatching a callback with a scope context raises `ValueError`.

## Nested operations and recursion

A hook may call memory again. `HookRegistry` tracks active callback pairs with
`ContextVar`. When the same callback is reached again recursively, only that one
callback is skipped. Other hooks and other callbacks still run.

```text
caller PUT
  -> AuditHook.after_put
       -> PUT audit block
            -> AuditHook.after_put skipped
            -> other callbacks still run
```

- **Each registry limits nesting depth for hook-originated work.** The default is 16, and going past it raises `HookRecursionError`.
- **The context is kept separate between threads and between asynchronous tasks,** and it is reset even when a callback raises. So concurrent agent runs never see one another's scopes.
- **A scope counts as a nesting level too.** Operations directly inside one report depth 1, and a scope opened from inside a hook is bounded by `max_depth` exactly like an operation.
- **To see only caller-initiated events,** return early when `context.origin == "hook"`. This also stops two hooks that create derived records from triggering each other again and again.

## Suppression

Trusted migrations or hook-internal code may suppress chosen hooks, or the whole
registry, in the current context:

```python
with memory.hooks.suppress(audit_hook, attributes={"migration": "m1"}):
    memory.put(type="audit", value={"event": "bootstrap"})

with memory.hooks.suppress(principal_id="ops:migrator"):
    memory.put(type="internal", value="no hooks")
```

**Turning hooks off is the one thing an audit trail cannot afford to lose. So a
suppression window is an operation, not a silent gap.** `suppress()` opens a
`HookOperation.SUPPRESS` context and yields it:

| Moment | What hooks receive |
| --- | --- |
| The window opens | `on_suppressed`, sent to every registered hook, including the ones that are about to go quiet. |
| Inside the window | Nothing, except for mandatory hooks, whose events report the window as their `parent_operation_id`. |
| The window closes | The terminal event of the suppression operation, sent to every registered hook. |

- **So a suppressed hook records that it was suppressed,** by whom, for what, and when it started reporting again, even though it recorded nothing in between. A mandatory hook also records what happened inside.
- **The `Suppression` value says how far the window reaches.** `selected` silences the hooks it lists. `registry` silences every hook that is not mandatory, including hooks registered while the window is open. `hooks` lists the hooks that stop reporting, and `mandatory` lists the ones that do not. Both are in dispatch order.

```python
class AuditHook(MemoryHook):
    def on_suppressed(self, context, suppression):
        alert(
            principal=context.principal_id,
            scope=suppression.scope,
            silenced=[type(hook).__name__ for hook in suppression.hooks],
            window=context.operation_id,
        )
```

- **Only registered, non-mandatory hooks can be suppressed.** Anything else raises `ValueError` before the window opens.
- **`HookRegistry.operation()` refuses `HookOperation.SUPPRESS`** for the same reason it refuses `HookOperation.SCOPE`, so nobody can fake a suppression window through the generic context.
- **Suppression is on the trusted registry only,** and not on `MemoryHandle` operations. That is deliberate.

## Authorization and failures

- **Hooks cannot widen normal access.** `before_put` cannot change the authorized parent, a changed search is authorized again, and the final search results are filtered again.
- **A changed handle search is checked against the handle again.** If `before_search` turns a handle search into a `global` or `ancestors` search that the handle does not allow, or moves the root outside the handle, the search raises `AccessDenied`.
- **Hooks are trusted extensions** with access to application data.
- **Dispatch is synchronous.** Genie does not retry hooks, and it does not include their external effects in backend transactions. Keep hooks fast, safe to repeat, and reliable.

What a failing hook costs depends on whether it can still change the outcome:

| Callbacks | A failure |
| --- | --- |
| `before_*` and `after_search` | Stops dispatch and fails the operation. |
| `after_*` returning `None`, `on_denied`, `on_operation_end` | Is collected; every hook still runs. |
| `on_suppressed` | Is collected; every hook still runs, then the window is refused. |

- **The commit point is the moment the change is written to storage.** A failure before it is a veto, so it has to stop everything. An aborted `put` stores nothing, and a failed `after_search` returns no results rather than results that a redaction hook never reviewed.
- **The observing callbacks report work that is already decided.** Stopping there cannot undo it, and it would only destroy the records that the remaining hooks and blocks are owed. So dispatch continues, and the collected failures are reported once it finishes.
- **How collected failures surface:** if the caller is already receiving an exception, they are attached to it as notes. Otherwise the first failure is raised, with the rest as notes on it, so that no failure hides another. A failing `on_denied` still lets the `AccessDenied` through, and a failing `on_operation_end` still lets the operation's own error through.
- **`on_suppressed` is the one exception, and it follows from the same reasoning.** It is isolated like the other observing callbacks, so every hook is told before anything is reported. But what it reports has not happened yet. A failure leaves dispatch enabled and does not run the suppressed body. A suppression that no hook could record is exactly what this callback exists to prevent.

### `fail_open`

A **sink** is wherever a hook sends its records, such as a log service or a
metrics service. A best-effort sink can declare that its own outages must not
reach callers:

```python
class AuditHook(MemoryHook):
    fail_open = True

    def after_put(self, context, block):
        self.sink.send(context.operation_id, block.id)
```

- **It covers only the observing callbacks,** for the same reason those callbacks are isolated.
- **`before_*` and `after_search` ignore it,** because containing a failure there would quietly store or return the value that the hook existed to change.
- **Contained failures are logged with their traceback** to the `genie.hooks.registry` logger, so a failing sink stays visible.
- **It also covers `on_suppressed`,** so a sink that called its own outages harmless cannot block a window either. A hook that needs the window recorded before it may proceed should not declare `fail_open`.

### Recursive deletion

- **`before_delete` runs for every block in the observed snapshot,** including descendants whose own policies were never consulted.
- **A concurrent change raises `MemoryConflict`.** Before hooks may already have run, but after hooks run only when the deletion succeeds.
- **Every deleted block reaches every hook before any failure surfaces.** So deleting five blocks with a sink that fails on the third still records five deletions.
- **Cascading backend cleanup** of links and structural references is represented by the block deletion itself rather than by separate structural events.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
[Queries](../query/README.md), and [Policies](../policy/README.md).
