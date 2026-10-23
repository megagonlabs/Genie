# Hooks

`genie.hooks` lets trusted application code watch or change logical memory
operations. Applications use it for audit records, metrics, tracing, redaction,
and index updates.

## Coverage

| Before | After | Operations |
| --- | --- | --- |
| `before_put` | `after_put` | Block creation |
| `before_update` | `after_update` | Block `set`, and dictionary or metadata `update` |
| `before_get` | `after_get` | Exact block lookup |
| `before_search` | `after_search` | Search |
| `before_traverse` | `after_traverse` | Children, ancestors, link and mount listing, and containment |
| `before_delete` | `after_delete` | Each block in a recursive delete |
| `before_add_link` | `after_add_link` | Regular, acyclic, and mount link creation |
| `before_update_link` | `after_update_link` | Link metadata update |
| `before_structure_change` | `after_structure_change` | Keys, log appends, and temporal membership |

3 callbacks have no before and after pair, and no one of them is tied to one
kind of operation:

- **`on_operation_end`** runs once for each operation that starts. This includes operations with no pair in the table above.
- **`on_denied`** runs each time Genie refuses a request.
- **`on_suppressed`** runs when trusted code turns off dispatch.

Other notes:

- **Structure events use immutable `StructureChange` values.** `kind` tells keyed collections, event logs, and temporal collections apart, and `action` names the exact change. Versioned records reuse the key and log events and send no duplicates.
- **No paired callbacks** for mount resolution, facade reads, handle creation, and direct backend calls.
- **Some operations start only to report a refusal.** Mount resolution, handle creation, and `require_access` open an operation only when they refuse. So hooks get `on_denied` and a `denied` terminal event for a refusal, and nothing for an allowed call. An allowed mount resolution reports at most the `get` of the target block.
- **`get` hooks are block lookups, not facade reads.** A facade can run ordinary `get` calls while it resolves blocks. So one high-level facade read is not one event.
- **Backend access is a trusted boundary.** It skips hooks and policy on purpose.

## Traversal

A read that follows relationships names a start block, not the blocks that it
returns. So one call can disclose many blocks.

- **Covered calls:** `children`, `ancestors`, `list_links`, `contains`, `list_mounts`, and `discover_mounts`.
- **Both callbacks get an immutable `Traversal`.** The before callback sees the read before it runs. The after callback sees the same read with what it disclosed.

| `kind` | `origin_id` | What the after callback gets |
| --- | --- | --- |
| `children` | Parent block | `blocks` |
| `ancestors` | Start block | `blocks` |
| `links` | Listed block | `links`, with the requested `relation` and `direction` |
| `contains` | Subtree root | `contained`, for the candidate in `target_id` |
| `mounts` | Source block | `links`, the mounts that leave that block |
| `mounts.subtree` | Scanned root | `links`, the mounts found anywhere below it |

- **`mounts` and `mounts.subtree` are separate kinds on purpose.** They return the same kind of record, but they read very different amounts of memory. A scan is one event that covers each readable block under `origin_id`. If a hook records the scan as a listing of one block, the hook reports much less than the scan disclosed. Both kinds report the reserved mount relation and the outgoing `direction` that they filtered on.
- **`include_self` is not in the event.** `discover_mounts(include_self=False)` does not scan the root block itself. `Traversal` has no field for this, so a hook cannot tell whether the scan included the root.

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

- **These callbacks observe a read. They do not make it narrower or wider.** `Memory` gets the traversal results from the backend and filters them with the `read` rule of the principal before the after callback sees them. A block that the principal cannot `read` is not in `blocks`, and `contains` reports `contained=False` for it and does not refuse. A `contains` call still refuses if the principal cannot `traverse_down` the root.
- **Structures walk the ownership tree through these operations, not around them.** So `OwnershipTree.descendants()` reports one `children` traversal for each block that it visits. A subtree read appears in the trail as the reads that made it.
- **The event reports the read that `Memory` performed.** A `MemoryHandle` hides links whose other endpoint is outside its root, and it does that after the read. So the link listing of a handle can report links that the caller never gets. The listing is never narrower than what the caller sees.

## Terminal events

Paired callbacks report work that happens. `on_operation_end` reports the
result of that work. It runs exactly once for each operation that starts, after
the other callbacks of that operation, with one of 3 outcomes.

| Outcome | Meaning |
| --- | --- |
| `success` | The operation finished, and any change is committed. |
| `denied` | A policy or a `MemoryHandle` boundary refused the operation with `AccessDenied`. |
| `error` | The operation failed for a different reason. |

- **It always runs.** So it is the correct place to close a tracing span (one timed unit of work in a trace), record the duration of an operation, and release state that a before callback saved.
- **It is the only callback that sees both refusals and failures.** A denied `set` never gets to `before_update`. A `set` that fails before `before_update`, for example on a block that does not exist or on an encode error, also never gets to it. A `set` that fails after `before_update` never gets to `after_update`.

```python
class MetricsHook(MemoryHook):
    def before_put(self, context, block):
        self.started[context.operation_id] = monotonic()
        return block

    def on_operation_end(self, context, outcome, error):
        started = self.started.pop(context.operation_id, None)
        record(context.operation, outcome, started)
```

- **A terminal event names the operation and the principal, not the target block.** `on_denied` gives the subject of a refusal, and the paired callbacks give the subject of successful work.
- **An operation that is rejected before it starts has no terminal event.** So a call above `max_depth` raises `HookRecursionError` without one.

## Refusals

`on_denied` reports each refusal that Genie raises, with the block that it
applies to and the check that caused it. It is only an observation. The refusal
stays, and a hook cannot undo it.

```python
class SecurityHook(MemoryHook):
    def on_denied(self, context, operation, subject_id, reason):
        alert(context.principal_id, context.operation, operation, subject_id)
```

- **The `operation` argument names what refused.** Built-in policy operations are the plain values `read`, `write`, `search`, `traverse_up`, and `traverse_down`. A custom operation that `require_access` checks arrives as its plain name. `MemoryHandle` boundaries are `HandleCapability` values with a namespace.
- **Built-in policy operations and handle boundaries never collide.** So the trail never shows an attempt to leave a sandbox as an ordinary permission failure.
- **`access.require` is a check with no change.** `Memory.require_access` and `MemoryHandle.require_access` run one policy check and report a refusal under that operation. They do not report an allowed check.

| Capability | Reason for the refusal |
| --- | --- |
| `handle.subtree` | The block is outside the handle `root`. |
| `handle.ancestors` | The handle has `allow_up=False`. |
| `handle.global_search` | The handle has `allow_global=False`. |
| `handle.mounts` | The handle has `allow_mounts=False`. |
| `handle.policy_override` | The call gave an explicit `policy`, and the handle does not have `allow_policy_override=True`. |
| `handle.custom_ids` | The call gave an `id`, and the handle does not have `allow_custom_ids=True`. |
| `handle.root_required` | An unbounded handle has no `root` to infer. |
| `handle.principal` | A child handle tried to change `principal`. |

- **The security boundary has complete coverage.** Genie reports policy refusals for block operations, search, links, mounts, traversal, listing, `contains`, handle creation, and `require_access` checks. Structure facades run these checks before they change membership. Genie reports handle refusals for each boundary and capability check, also for child handles that try to widen access. Each refusal comes with a `denied` terminal event that has the same operation id.
- **A refusal can be too deep to report.** Mount resolution, handle creation, and `require_access` open an operation to report a refusal. If that operation starts inside a hook above `max_depth`, hooks do not get the refusal, and the `AccessDenied` carries a note about it.
- **Ordinary visibility filters are not refusals.** Search and traversal leave out results that a principal cannot read, and Genie reports nothing about them. So `on_denied` stays a signal and does not become a stream.
- **Genie does not report refusals inside an `OwnershipTree` facade root.** That bound is a view over an access object that the caller already holds, so an exit from it cannot widen access. The underlying handle and policy still refuse.

## Writing a hook

Hooks run in registration order. A callback that transforms passes its result
to the next hook.

```python
from genie import HookContext, Memory, MemoryHook


class AuditHook(MemoryHook):
    def after_update(self, context: HookContext, previous, stored):
        print(context.operation_id, context.operation, stored.id)


audit_hook = AuditHook()
memory = Memory(hooks=[audit_hook])
```

**Each callback gets a `HookContext`.** It holds these values:

- **`operation_id`:** one id that the before and after callbacks share.
- **`parent_operation_id`:** the id of the parent operation or scope, or `None`.
- **`operation`:** the operation type.
- **`principal_id`:** the id of the acting principal.
- **`depth`:** a zero-based depth.
- **`origin`:** `caller` or `hook`.
- **`attributes`:** the inherited string labels.
- **`started_at`:** the start timestamp of the operation.

Where the context comes from:

- **`Memory` and the structure facades create this context** and pass it on.
- **Code that dispatches through `HookRegistry` directly** uses the context that `HookRegistry.operation()` yields, because dispatch does not create an operation by itself. A dispatch with any other context raises `ValueError`.

### What a hook may change

| Hook | Can change |
| --- | --- |
| Block update | The value, the metadata, and the content type |
| Link | The metadata only |
| Block creation (`before_put`) | The value, the metadata, the content type, and the `policy` |
| Structural | Nothing. They observe immutable records that belong to the backend |

- **Genie checks the protected fields before it stores anything:** identity, ownership, topology, policy, timestamp, and version.
- **Each `MemoryBlock` field that is not in the table is protected.** This includes `id`, `type`, `parent_id`, `created_by`, `created_at`, `updated_at`, and `version`. A change to one raises `ValueError`, and Genie stores nothing.
- **A `before_put` policy is a `MemoryPolicy`.** Any other value raises `TypeError`, and Genie stores nothing.
- **`before_put` is different from `before_update`,** because Genie sets a policy when it creates a block, and the policy does not change after that. `before_put` can set the `policy` of the new block. `before_update` cannot.
- **`before_put` still cannot change the authorized parent.**

### `before_put` is the only per-block policy choice

- **This applies to writes through a `MemoryHandle`.** Trusted code that holds `Memory` can also give an explicit `policy` to `Memory.put`.
- **A `MemoryHandle` refuses explicit policies,** unless it was created with `allow_policy_override=True`.
- **`Memory.default_policy` is one policy for each write that gives no `policy`.**
- **A hook sees the block and the `HookContext`.** So it can choose from the block type, the metadata, the acting principal, or the attributes of the scope that contains the operation:

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

- **A hook that sets policies is part of the access control.** With a mandatory registration, later code cannot detach it. The hook belongs in trusted code.
- **Its decisions also belong in the audit trail.** Without them, a reader of the trail cannot tell why a block has its policy.

## The registry

`HookRegistry` owns the dispatch order, and the list of installed hooks is also
auditable. `hooks` lists them, so startup code can assert that the trail is in
place:

```python
registry = HookRegistry()
registry.register(audit_hook, mandatory=True)
memory = Memory(hooks=registry, attributes={"service": "memory"})

assert audit_hook in memory.hooks.hooks
assert memory.hooks.mandatory == (audit_hook,)
```

- **The registry identifies hooks by object identity.** So 2 instances of the same class are 2 hooks.
- **A second registration of one instance raises `ValueError`.** Without this check, the registry quietly reports each operation twice.
- **`unregister` removes a hook by the same identity.** It raises `ValueError` if that hook is not installed.
- **A new registration changes the order.** A hook that is registered again later goes to the end.

| Attempt | Ordinary hook | Mandatory hook |
| --- | --- | --- |
| `unregister(hook)` | Removed | `ValueError` |
| `suppress(hook)` | Silent for the window | `ValueError` |
| `suppress()` | Silent for the window | Continues to report |
| `memory.hooks = other` | `AttributeError` | `AttributeError` |

- **`mandatory=True` means that the hook cannot be unregistered or suppressed.** So an audit hook stays in place for all code that runs after it.
- **`Memory.hooks` is read-only.** So the registry of a memory is fixed for its whole lifetime. To change which hooks run, code registers or unregisters hooks on that registry, and a mandatory hook stays in place through that. Code cannot replace the whole registry to get around this.

## Scopes and attributes

Genie names the operations that it performs. Applications name the work that
causes them, such as an agent run, a turn, or a tool call. `HookRegistry.scope()`
groups the operations of one caller-defined span and gives them a label:

```python
with memory.hooks.scope("agent.turn", attributes={"run_id": run, "turn": "7"}):
    handle.put(type="note", value=draft)
    handle.search("open questions")
```

- **A scope dispatches no callbacks.** It opens a causal context. Operations that start inside it become its children, and they inherit its attributes at any depth. So a hook can group work by label with no knowledge of how the calls nest:

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

- **Attributes are a read-only mapping of strings to strings.** Each context inherits the attributes of the operation or scope that contains it. If there is a conflict, the attributes that the operation itself adds win.
- **`Memory` adds the attributes from its configuration:** `Memory(hooks=[audit_hook], attributes={"service": "memory"})`.

A `MemoryHandle` adds its own boundary to those attributes:

| Attribute | Value |
| --- | --- |
| `handle.id` | Stable id of the handle that performed the operation. |
| `handle.root` | Handle root. An unbounded handle does not have this attribute. |
| `handle.capabilities` | The enabled `ancestors`, `global_search`, `mounts`, `policy_override`, and `custom_ids` capabilities, separated by commas. An empty string if no capability is on. |

- **So the trail can tell apart 2 handles with the same principal.** This includes the refusal that Genie reports when one handle tries to leave its sandbox.
- **Attributes describe the causal chain, not only the acting object.** A derived write that a hook performs inside a handle operation inherits the labels of that handle. `origin` and `depth` tell work that the caller started apart from work that a hook caused.
- **Scope contexts stay outside the callback contract.** A scope reports the `HookOperation.SCOPE` operation, and its free-form name is in `scope_name`. Each operation that Genie performs reports its own `HookOperation` and a `scope_name` of `None`. So `context.operation` is always a strongly typed `HookOperation`. A context is a named span or performed work, never both.
- **Hooks never get a scope context.** `HookRegistry.operation()` refuses `HookOperation.SCOPE`, and a callback dispatch with a scope context raises `ValueError`.

## Nested operations and recursion

A hook can call memory again. `HookRegistry` keeps the active pairs of hook and
callback in a `ContextVar`. If a callback starts again inside its own call, the
registry skips only that callback. Other hooks and other callbacks still run.

```text
caller PUT
  -> AuditHook.after_put
       -> PUT audit block
            -> AuditHook.after_put skipped
            -> other callbacks still run
```

- **Each registry limits the depth of work that starts in hooks.** The default `max_depth` is 16. A call above it raises `HookRecursionError`.
- **The context is separate for each thread and each asynchronous task.** The registry resets it also when a callback raises. So concurrent agent runs never see the scopes of each other.
- **A scope also counts as a level.** Operations directly inside a scope report depth 1. A scope that opens inside a hook has the same `max_depth` limit as an operation.
- **For only the events that callers started,** a hook returns early when `context.origin == "hook"`. This also stops a loop in which 2 hooks that create derived records start each other again and again.

## Suppression

Trusted migrations or code inside hooks can suppress selected hooks, or the
whole registry, in the current context:

```python
with memory.hooks.suppress(audit_hook, attributes={"migration": "m1"}):
    memory.put(type="audit", value={"event": "bootstrap"})

with memory.hooks.suppress(principal_id="ops:migrator"):
    memory.put(type="internal", value="no hooks")
```

**An audit trail cannot lose the record that hooks are off. So a suppression
window is an operation, not a silent gap.** `suppress()` opens a
`HookOperation.SUPPRESS` context and yields it:

| Moment | What hooks get |
| --- | --- |
| The window opens | `on_suppressed`, sent to each registered hook, also to the hooks that will no longer report. |
| Inside the window | Nothing, except for mandatory hooks. Their events report the window as their `parent_operation_id`. |
| The window closes | The terminal event of the suppression operation, sent to each registered hook. |

- **So a suppressed hook records that it was suppressed:** by whom, for what, and when it started to report again. It records nothing in between. A mandatory hook also records what happened inside.
- **The `Suppression` value tells how far the window reaches.** `selected` silences the hooks that it lists. `registry` silences each hook that is not mandatory, also hooks that are registered while the window is open. `hooks` lists the hooks that no longer report, and `mandatory` lists the hooks that continue. Both lists are in dispatch order.
- **Nested windows:** a hook that an outer window silences gets no events from an inner window.

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

- **Only registered hooks that are not mandatory can be suppressed.** Any other hook raises `ValueError` before the window opens.
- **`HookRegistry.operation()` refuses `HookOperation.SUPPRESS`** for the same reason that it refuses `HookOperation.SCOPE`. So no code can fake a suppression window through the generic context.
- **Suppression is on the trusted registry only,** and not on `MemoryHandle` operations. This is on purpose.

## Authorization and failures

- **Hooks cannot widen normal access.** `before_put` cannot change the authorized parent. Genie authorizes a changed search again, and it filters the final search results again.
- **Genie checks a changed handle search against the handle again.** If `before_search` turns a handle search into a `global` or `ancestors` search that the handle does not allow, or moves the root outside the handle, the search raises `AccessDenied`.
- **Hooks are trusted extensions** with access to application data.
- **Dispatch is synchronous.** Genie does not retry hooks, and it does not include their external effects in backend transactions. So a hook that is slow, not safe to repeat, or not reliable has a direct effect on each operation that dispatches it.

What a hook failure costs depends on whether the hook can still change the
outcome:

| Callbacks | A failure |
| --- | --- |
| `before_*` and `after_search` | Stops dispatch and fails the operation. |
| `after_*` that return `None`, `on_denied`, `on_operation_end` | The registry collects it, and each hook still runs. |
| `on_suppressed` | The registry collects it, and each hook still runs. Then the registry refuses the window. |

- **The commit point is the moment when the backend writes the change to storage.** A failure before it is a veto, so it stops everything. An aborted `put` stores nothing. A failed `after_search` returns no results, and not results that a redaction hook never reviewed.
- **The observing callbacks report work that is already decided.** A stop there cannot undo the work. It only removes the records of the other hooks and blocks. So dispatch continues, and the registry reports the collected failures when dispatch is complete.
- **The registry collects only `Exception` failures.** A `BaseException`, such as `KeyboardInterrupt`, stops dispatch at once.
- **How the registry reports collected failures:** if the caller already gets an exception, the registry attaches the failures to it as notes. If not, it raises the first failure, with the others as notes on it, so that no failure hides another. A failed `on_denied` still lets the `AccessDenied` through, and a failed `on_operation_end` still lets the error of the operation through.
- **`on_suppressed` is the one exception, and the same reason explains it.** It is isolated like the other observing callbacks, so each hook gets it before the registry reports anything. But the work that it reports has not happened yet. A failure keeps dispatch on and does not run the suppressed body. This callback exists to prevent a suppression that no hook can record.

### `fail_open`

A **sink** is the place where a hook sends its records, such as a log service
or a metrics service. A best-effort sink can declare that its own outages do
not get to callers:

```python
class AuditHook(MemoryHook):
    fail_open = True

    def after_put(self, context, block):
        self.sink.send(context.operation_id, block.id)
```

- **It covers only the observing callbacks,** for the same reason that those callbacks are isolated.
- **`before_*` and `after_search` ignore it.** If the registry contained a failure there, it would quietly store or return the value that the hook exists to change.
- **The registry logs contained failures with their traceback** to the `genie.hooks.registry` logger. So a failed sink stays visible.
- **It also covers `on_suppressed`.** So a sink that declares its own outages harmless cannot block a window either. A hook that needs a record of the window before the window opens does not declare `fail_open`.

### Recursive deletion

- **`before_delete` runs for each block in the observed snapshot.** This includes descendants whose own policies Genie never checked.
- **Each block gets to the hooks decoded.** So the `Memory` that deletes needs a codec for each block in the subtree. If a codec is missing, `delete` raises `TypeError` before any hook runs, and the backend deletes nothing.
- **A concurrent change raises `MemoryConflict`.** Before hooks can already have run, but after hooks run only when the delete succeeds.
- **Each deleted block gets to each hook before any failure surfaces.** So a delete of 5 blocks with a sink that fails on the third block still records 5 deletions.
- **Cascade cleanup in the backend** of links and structural references sends no separate structural events. The block delete itself stands for that cleanup.

Related: [Memory](../memory/README.md), [Structures](../structures/README.md),
[Queries](../query/README.md), and [Policies](../policy/README.md).
