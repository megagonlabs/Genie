# Detailed Architecture

This note shows how each module works inside. It covers the stored shapes, the
order of checks, the algorithms, and the invariants that each change keeps.
[High-level architecture](03-high-level-architecture.md) shows the components
and their boundaries.

## Stored shapes

### `MemoryBlock`

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `id` | `str` | Caller or `new_memory_id()` | `mem_` and a `uuid4` hex string |
| `type` | `str` | Application | Genie does not interpret it |
| `value` | `Any` | Codec | The codec encodes it for storage and decodes it on read |
| `parent_id` | `str \| None` | Caller at creation | The only ownership field |
| `content_type` | `str` | Codec | Selects the codec that decodes `value` |
| `metadata` | `dict` | Caller | Always strict JSON, for all value codecs |
| `policy` | `MemoryPolicy \| None` | Creation only | Genie reads `None` as `owner_private()` |
| `created_at`, `updated_at` | `datetime` | `Memory` | Timezone-aware UTC |
| `created_by` | `str \| None` | `Memory` | The id of the principal that did the `put`. `AccessRule` calls this principal the "owner" |
| `version` | `int` | `Memory` | Starts at 1 and increases by exactly 1 for each update |

- **Each model is a frozen dataclass with `slots=True`.** A change makes a new
  object through `MemoryBlock.with_updates(...)` or `dataclasses.replace`. So
  no code can edit an object in place after Genie returns it.
- **`with_updates(metadata=None)` writes `{}`,** not `None`, so metadata is
  always present.
- **The constructors check the identity fields.** `MemoryBlock` checks `id`,
  `type`, and `parent_id`. `MemoryLink` checks `id`, `source_id`, `target_id`,
  and `relation`. A value that is not a string raises `TypeError`. An empty
  string raises `ValueError`. `parent_id` can be `None`. `with_updates` and
  `dataclasses.replace` run the same check.
- **Each id starts with a prefix for its kind:** `mem_` for blocks, `link_` for
  links, `handle_` for handles, and `hookop_` for hook contexts. The prefix
  shows the kind of an unknown id in a log.

### `MemoryLink`

- **Fields:** `source_id`, `target_id`, `relation`, `id`, `metadata`,
  `created_at`. A link has no version and no update timestamp.
- **Only `metadata` can change,** and only through a merge.
  `Memory.update_link` builds the link again with the merged metadata. The
  backend rejects a change to any other field.
- **`LinkRelations.MOUNT` (`"genie.mount"`) is reserved.** It marks a mount.
  `add_acyclic_link` and `protect_relation` refuse it.

## The `Memory` operation template

Each public method of `Memory` uses the same sequence of steps:

```text
with self._operation(HookOperation.X) as context:   # 1 open a context
    block = self._require_raw(id)                   # 2 read the encoded state
    self._require_access(context, OP, block)        # 3 authorize, and report a refusal
    payload = self.codecs.encode(...)               # 4 encode the value (writes only)
    payload = self.hooks.before_x(context, payload) # 5 let hooks change the payload
    _require_..._invariants(proposed, payload)      # 6 check the hook changes
    stored = self.backend.write_or_read(payload)    # 7 make one backend call
    self.hooks.after_x(context, stored)             # 8 report the outcome
    return self._decode_block(stored)               # 9 decode the result
```

- **`_require_raw` reads the encoded block** directly from the backend. It
  raises `MemoryNotFound` if the block does not exist. Authorization always
  uses the stored state, not a block object that the caller gives.
- **`_require_access` reports before it raises.** It sends a refusal to
  `on_denied` with the operation, the block id, and the message. Then it raises
  `AccessDenied`. The exception leaves the operation, so the terminal event of
  the operation is `HookOutcome.DENIED`.
- **Each operation makes exactly one backend write.** Genie has no second
  write that undoes a bad first write. For this reason, `Memory` validates the
  hook changes before the write, not after it.

### Authorization per operation

| Operation | Checked on | Required |
| --- | --- | --- |
| `put` | the parent block, if there is one | `write` and `traverse_down` |
| `get` | the block | `read` |
| `set`, `update` | the block | `write`, then `read` |
| `delete` | the target block only | `write` |
| `children` | the parent | `traverse_down`, then `read` filters each child |
| `ancestors` | the block | `traverse_up`, then `read` filters each ancestor |
| `contains` | the root | `traverse_down`, and `read` on the candidate |
| `add_link`, `add_acyclic_link`, `add_mount` | both endpoints | `write` on the source, `read` on the target |
| `update_link` | both endpoints | `write` and then `read` on the source, and `read` on the target |
| `protect_relation` | none | No policy check. Only `Memory` has it, and the protection covers the whole backend |
| `list_links`, `list_mounts` | the block | `read`, and `read` on both endpoints of each link |
| `discover_mounts` | the subtree root | `search` and `traverse_down`, and `read` for each source block |
| `resolve_mount` | the mount source | `read`, then `get` checks the target |
| `search` | the query root | `search`, and the traversal right of the scope |
| `get_handle` | the handle root | `read`, **as the principal of the new handle** |

- **A refused list is a shorter list.** `children`, `ancestors`, `list_links`,
  and search remove each item that the principal cannot read. They do not
  raise an exception.
- **A refused call for one block raises.** `get` separates 2 cases. For a
  block that does not exist, `get` returns `None` before a policy check runs.
  For a block that exists but that the principal cannot read, `get` raises
  `AccessDenied`.
- **`delete` checks only the target.** `delete` does not check the policies of
  the descendants. Write access to a container already gives authority over
  the blocks that it contains.
- **A call that returns stored content needs `read`.** `set`, `update`, and
  `update_link` return the stored content after the write. So they check
  `read` after `write`. A principal that can write a block but cannot read it
  cannot change the block in place.
- **An application builds a write-only inbox with `put`.** To add a block under
  a container, `put` needs `write` and `traverse_down` on the container, not
  `read`. `put` returns only the new block.

### Deletion

1. **Read the target** and check `write`.
2. **Ask the traversal index** for `descendants(id, include_self=True)`. If
   the target in that snapshot is not the block that step 1 authorized, at
   the same version, `Memory` raises `MemoryConflict`.
3. **Dispatch `before_delete` one time for each block** in that snapshot.
4. **Call `delete_block(id, expected_blocks=[...])`** with the exact
   snapshot. If the subtree gets a new block, loses a block, or has a
   changed block after step 2, the adapter fails with `MemoryConflict`. A
   block that is deleted and created again with the same id is a changed
   block.
5. **Dispatch `after_delete(*blocks)`** with all removed blocks in one call.

- **`Memory` does not remove dependent state.** The adapter removes the links,
  key bindings, log entries, and temporal entries of the removed blocks inside
  `delete_block`. The adapter does these removals in the same atomic step as
  the block delete.
- **The traversal index defines the subtree.** If an index omits a descendant,
  the snapshot is incomplete. A correct `BlockStore` rejects an incomplete
  snapshot.

### Update paths

- **`set` replaces the value.** It encodes the new value with the
  `content_type` of the caller, or it selects a codec again. So `set` can
  change the content type of a block.
- **`update` merges.** It decodes the stored value. If the caller gives
  `value=`, `update` needs a `dict` as the stored value. It merges both `value`
  and `metadata`, and encodes the result with the current content type of the
  block.
- **Both increase `version` by 1** and give `expected_version=block.version` to
  the backend. So if 2 writers change the block at the same time, the second
  writer gets `MemoryConflict`.

## Codec boundary

- **Dispatch on encode:** An explicit `content_type` selects a registered codec
  directly. If no codec is registered for that content type, the registry
  raises `TypeError`. Without an explicit `content_type`, the registry examines
  its codecs in registration order. It uses the first codec whose `can_encode`
  returns `True`.
- **Dispatch on decode:** The registry uses only `EncodedValue.content_type`.
  If the codec that stored a block is not registered now, the decode raises
  `TypeError`. So a store and the registry configuration that wrote it stay
  together.
- **`JsonCodec` is the default registered codec.** It accepts `None`, `str`,
  `bool`, `int`, finite `float`, `list`, and `dict` with string keys. It
  rejects `Enum` values, so `EnumCodec` can encode them.
- **`JsonCodec` limits nesting to `MAX_NESTING_DEPTH` (100) levels.** Its check
  uses its own stack. So a value that is too deep raises `ValueError`, not
  `RecursionError`. The limit keeps the later recursive steps far below the
  Python recursion limit. These steps are the deep copies and the JSON checks
  of the adapters. `can_encode` returns `True` for a value that is too deep. So
  registry dispatch reports the `ValueError`, and not "no codec".
- **The registry validates codec results.** If a codec returns a value that is
  not an `EncodedValue`, the registry raises `TypeError`. If a codec returns an
  `EncodedValue` with a different content type, the registry raises
  `ValueError`. So a codec error never becomes a block with the wrong type
  silently.
- **Codecs do not handle metadata.** `_copy_metadata` always validates and
  copies metadata through `JsonCodec`, for all value codecs. So filters and
  adapter indexes always get strict JSON.
- **Encode and decode both make deep copies.** A caller cannot change stored
  state through a returned value. A caller also cannot change a stored value
  through a change to the value that it gave.
- **`genie.codecs.stdlib` adds opt-in codecs** for `bytes` (base64),
  `datetime`, `Decimal`, `UUID`, dataclasses, and enums. Each codec has its own
  `application/vnd.genie.*` content type. The default registry does not
  register them.

## Policy evaluation

`PolicyEngine.allows(operation, block, principal)` makes its decision in this
order:

1. **Type checks** on the block, the principal, and the policy.
2. **Policy resolution:** `block.policy`, or `MemoryPolicy.owner_private()` if
   the block has no policy.
3. **Rule lookup:**
   - For a built-in name (`read`, `write`, `search`, `traverse_up`,
     `traverse_down`), the engine reads the `MemoryPolicy` field with that
     name.
   - For other names, the engine reads `policy.operations[name]` of the block.
   - If the block policy has no rule for the name, the engine uses its default
     from `PolicyEngine(operations=...)`.
   - If the engine also has no rule, it raises `ValueError`. So a typo never
     allows access silently.
4. **`AccessRule.allows`:** The rule allows access at the first match, in
   this order: `allow_all`, the owner, an explicit principal id, and then a
   shared role. The owner matches if `allow_owner` is set and
   `principal.id == block.created_by`. A shared role is a role that both the
   principal and the rule have.

- **`require` calls `allows` and raises if the answer is no.** Each filtered
  read calls `allows` directly. So it can remove a block from the result and
  not fail.
- **A custom operation name cannot replace a built-in name.**
  `_freeze_operation_rules` rejects such a name at construction, for both the
  policy and the engine.
- **Genie freezes and copies rules and policies.** Sets become `frozenset`.
  Operation maps become a read-only mapping. Principal metadata gets a deep
  copy. So a policy cannot change after a block has it.

## `MemoryHandle` internals

- **A handle has no state other than its boundary:** the `Memory`, a root id,
  a principal, a default scope, 4 capability flags, and a frozen attribute
  map.
- **`_as_principal()` builds a derived `Memory`.** The derived `Memory` shares
  the backend, the policy engine, the hook registry, the codec registry, and
  the default policy. But it acts as the principal of the handle, and it
  carries the attributes of the handle. Each handle method does its own
  boundary check and then calls the derived object. For this reason, the trail
  shows the id and the capabilities of the handle for work done through that
  handle.
- **The handle checks the boundary first and the policy second.**
  `_require_in_subtree` uses `backend.contains(root_id, block_id)` without a
  policy check. So a rooted handle refuses an unknown id as "outside the
  handle". This refusal occurs before a lookup, so it does not show if the
  block exists.
- **A mount target can be outside the root.** For such a target,
  `_require_readable_mount_target` reads the block and checks `read` without
  a report. A missing target and an unreadable target get the same `SUBTREE`
  refusal. So `add_mount` does not show if the target exists.
- **Other ids get the boundary check before the lookup.** A search root must
  be inside the root for each scope, also `global`. `update_link` checks the
  source id of the link before the link lookup.
- **`_deny` opens its own operation.** A handle refusal occurs before the
  `Memory` operation starts. So the handle opens one context and sends
  `on_denied` with the `HandleCapability` that refused. Then the terminal event
  closes the context. If the registry cannot report the refusal, the handle
  attaches the reason to the `AccessDenied` as a note. The caller still gets
  the refusal.
- **The handle narrows search 2 times.** First, the handle builds the query
  again with the root and scope that it checked. So a `MemoryQuery` that the
  caller built cannot add a wider root or scope. Second, the handle
  filters the results again with `contains`, for all scopes except `global`
  and `ancestors`.
- **The handle checks a rewritten search again.** The handle calls
  `Memory._search` with a `boundary` check. This check runs inside the search
  operation, on the query that `before_search` returns. It refuses the same
  scopes and roots as the first check. It reports each refusal to `on_denied`
  in that operation. The last `contains` filter uses the scope of the query
  that ran, not the scope that the caller asked for.

| Handle capability | Flag | What it gates |
| --- | --- | --- |
| `SUBTREE` | always on for a rooted handle | Refuses each id outside the root |
| `ANCESTORS` | `allow_up` | `ancestors()` and `scope="ancestors"` |
| `GLOBAL_SEARCH` | `allow_global` | `scope="global"` |
| `MOUNTS` | `allow_mounts` | Calls that create, list, or resolve mounts. A mount target can be outside the root if the principal can read it. So the handle can read each block that its principal can read, if it knows the id |
| `POLICY_OVERRIDE` | `allow_policy_override` | An explicit `policy` argument to `put` |
| `CUSTOM_IDS` | `allow_custom_ids` | An explicit `id` argument to `put`. Block ids are unique in the whole backend, so a taken id shows that a block exists outside the root |
| `ROOT_REQUIRED` | unbounded handles | Refuses a call if Genie needs to infer its root |
| `PRINCIPAL` | child handles | Refuses a change of principal |

- **`get_handle` only narrows.** It refuses a flag that the parent does not
  have, a root outside the root of the parent, and a different principal.
- **The handle validates `default_scope` against the flags** at construction.
  So no handle has a default scope that its own flags refuse.

## Hook registry internals

### Context

- **4 `ContextVar` values hold the state:** the current context, the set of
  active `(hook, callback)` pairs, the suppressed registries, and the
  suppressed `(registry, hook)` pairs. These values are local to a context. So
  one thread or task does not see the suppression window of another thread or
  task.
- **`HookContext` is a node in a tree:** `operation_id`, `parent_operation_id`,
  `operation`, `origin` (`"caller"` or `"hook"`), `depth`, `principal_id`,
  inherited `attributes`, and `scope_name`.
- **Attributes merge down the tree,** and the inner attributes take priority.
  The boundary attributes of a handle take priority over the attributes of the
  memory. An added attribute takes priority over an inherited attribute.
- **`scope()` groups operations and does not dispatch.** It opens a context
  under `HookOperation.SCOPE`. No callback receives this context. The
  operations inside it report it as their parent and inherit its attributes.
- **`max_depth` limits only the work that hooks start.** `origin` is `"hook"`
  if a callback is on the stack. In that case only, a depth above `max_depth`
  raises `HookRecursionError`. The refused operation does not start, so it
  sends no terminal event.

### Dispatch

| Kind | Callbacks | Behavior on failure |
| --- | --- | --- |
| Mutating, fail-fast | every `before_*`, `after_search` | Stop at the first failure. `fail_open` has no effect |
| Observing, isolated | `after_put`, `after_update`, `after_get`, `after_traverse`, `after_add_link`, `after_update_link`, `after_protect_relation`, `after_structure_change`, `after_delete`, `on_denied`, `on_suppressed`, `on_operation_end` | Run all hooks, then report the failures |

- **Mutating callbacks form a chain.** Each hook receives the return value of
  the previous hook. The registry checks the return type (`MemoryBlock`,
  `MemoryQuery`, `MemoryLink`, or a list of `SearchResult`).
- **Isolated callbacks report work that already occurred.** An early stop does
  not undo the work. It only takes the record away from the hooks that did not run.
  So the registry collects the failures. If an exception already leaves the
  operation, the registry attaches the failures to it as notes. It also logs
  them, because code can catch that exception and drop it. A structure listing
  drops the refusal of each hidden member, because a raised error would show
  that the member exists. If the operation succeeded, the registry raises the
  first failure and attaches the others to it as notes.
- **The registry logs and ignores the failure of a `fail_open` hook,** but only
  for isolated callbacks.
- **The registry skips a reentrant call and does not refuse it.** `_invoke`
  uses `(id(hook), callback_name)` as the key. If the callback of a hook
  enters the same callback again, that call gets `_SKIPPED`. For a mutating
  callback, the value then passes through without change.
- **The registry takes a snapshot of the dispatch order for each event.** If a
  hook registers or unregisters in a callback, the change applies to the next
  event. It does not apply to the current event.
- **Registration uses object identity,** and the registry rejects a second
  registration of the same object. So one hook cannot report one operation 2
  times.

### Suppression

- **A suppression window is also an operation** (`HookOperation.SUPPRESS`).
  So the trail shows the gap, and the events do not simply disappear.
- **Order:** plan the window, open the operation, and dispatch `on_suppressed`
  to all hooks. This includes the hooks that the window will silence. Then
  disable dispatch. A failure in `on_suppressed` refuses the window, and the
  body does not run.
- **2 scopes:** `REGISTRY` silences all hooks that are not mandatory. This
  includes hooks that register while the window is open. `SELECTED` silences
  the named hooks. The name of an unregistered hook or of a mandatory hook
  raises `ValueError`.
- **Mandatory hooks always run.** They see the window, the operations inside
  it, and its terminal event. The registry cannot unregister a mandatory hook.

## Search pipeline

1. **Convert** a string, keyword arguments, or a `MemoryQuery` into one
   normalized `MemoryQuery`.
2. **Authorize the root:** `search`, and also `traverse_down` for `subtree`,
   `traverse_up` for `ancestors`, and `read` for `links`. A scope other than
   `global` with no root raises `ValueError`.
3. **Run `before_search`.** Then normalize the filters again and **authorize
   again**, because a hook can return a new query. For a handle search, the
   `boundary` check of the handle runs first on the changed query.
4. **Get backend pages** in `_collect_visible_results`. The backend
   resolves the scope, finds the matches, and returns at most the batch size
   of results after the cursor, in search order.
5. **Filter** each result with `_is_visible_result`, and **decode** only the
   results that it keeps. Continue until there are `query.limit` visible
   results.
6. **Run `after_search`**, then **filter again** with the same verdict cache.

`_can_return_search_result` reads the stored block again by id. It checks
`search`, then `read`, then scope membership:

| Scope | Membership test |
| --- | --- |
| `global` | Always true |
| `self` | `block_id == root_id` |
| `subtree` | `backend.contains(root_id, block_id)` |
| `ancestors` | `block_id == root_id` or `backend.contains(block_id, root_id)` |
| `links` | `block_id == root_id`, or a stored link in either direction connects them |

- **A handle adds one check.** `MemoryHandle` gives a `keep` check that
  removes `self`, `subtree`, and `links` results outside the handle root. It
  runs before the count, so these results do not use part of the limit.
- **One verdict for each block id in each call.** The backend pages and the
  filter after `after_search` share a dict, so `Memory` reads each block one
  time. A verdict depends only on the id and the stored block, never on the
  other results. So a reused verdict is as strict as a new check.
- **A side effect:** if a hook deletes a block inside `after_search`, the
  result for that block stays on the current page.
- **`limit` counts visible results.** So blocks that the caller cannot see
  do not change which visible results it gets.
- **Search order:** `score` from high to low, then `created_at` from old to
  new, then block id. `page_results` applies it in each shipped adapter.
- **Page size:** the first backend page asks for `query.limit` results. Each
  next page starts after the `cursor` of the last result and asks for 2 times
  as many. So the number of backend calls grows with the log of the number of
  hidden matches.
- **End:** the loop stops at `query.limit` visible results, or when a page is
  shorter than its size. There is no cap, because a cap would let hidden
  blocks change the results again.
- **Duplicates:** a block that changes between 2 pages can move past the
  cursor and come back. Only its first result counts.
- **A backend that ignores the cursor** still lets the loop end, because each
  page is larger than the one before. It costs more calls.

## Query normalization

- **`MemoryQuery` normalizes its fields in `__init__`,** and it is frozen after
  that. A scope that is not `None` becomes a `Scope`. `None` means the
  default scope of the caller, and `Memory` sets it before hooks and backends
  see the query. The types become a tuple. `MemoryQuery`
  rewrites the filters. A negative limit raises `ValueError`, but `0` is
  permitted.
- **`MemoryQuery` rewrites operator aliases** to the canonical names that
  adapters receive:

| Alias | Canonical |
| --- | --- |
| `==`, `=` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

- **`matches_filters` resolves a field name in 3 steps:** A name with the
  `metadata.` prefix reads metadata. Then the name of a real `MemoryBlock`
  field reads that field. All other names read metadata.
- **An incompatible comparison is `False`, not an error.** The matcher catches
  a `TypeError` from a comparison of different types. An empty operator map
  does not match. So a filter never matches by accident.
- **Genie rejects an unknown operator before the match.** `normalize_filters`
  raises `ValueError` for it when the query is built. So a typo such as `>>=`
  raises an error and does not return an empty result. An adapter can give the
  matcher a filter that did not go through normalization. In that case, the
  matcher treats an unknown operator as no match.
- **This matcher is the portable minimum.** `InMemoryBackend` and the shipped
  adapters use it for the part of a filter that they do not translate into
  their own query language. An adapter can implement more, if the same query
  keeps its meaning.

## Structure facade internals

### Construction

1. **Resolve the container through the access object.** A missing container
   raises `MemoryNotFound` through a `Memory`. A container that the caller
   cannot read, or that is outside the handle root, raises `AccessDenied`. A
   rooted handle also raises `AccessDenied` for a missing container, because
   it checks its root before it gets the block.
2. **Resolve the store.** `resolve_store` returns the store without change if
   it is not a `BlockRouter`. For a `BlockRouter`, the facade binds to the
   backend that owns the container block. A container and its subtree use one
   route.
3. **Check the capability.** The facade uses `isinstance` with the
   `runtime_checkable` protocol. If the store does not have the capability,
   the facade raises `UnsupportedOperation`. This check occurs at construction, not at
   the first use.
4. **Resolve the hooks:** the explicit registry, or `access.hooks`, or the
   registry of the `_memory` of a handle. A facade without a registry uses
   `nullcontext` and dispatches no events.

- **The container comes before the store.** The route lookup of a
  `BlockRouter` reads the backend directly, with no handle or policy check. If
  it ran first, a rooted handle would raise `MemoryNotFound` for a missing id
  outside its root, and `AccessDenied` for an id that exists. The error would
  then show whether the block exists.
- **`VersionedRecords` gets the container 2 times.** It gets it once for its
  own checks, and its inner `KeyedCollection` gets it again. So the hooks see
  2 `get` events.

### Writing

- **Each change opens its own hook operation** (`key.bind`, `key.rebind`,
  `key.unbind`, `log.append`, `temporal.add`, `temporal.replace`,
  `temporal.remove`). It reports a `StructureChange` with `kind`, `action`,
  `container_id`, `block_id`, `before`, and `after`.
- **`after_structure_change` carries the stored record,** not the proposed
  record. So the trail shows the allocated sequence or the stored binding.
- **The backend does the atomic step.** The facade prepares the change and
  reports it. The uniqueness check, the compare-and-swap, and the sequence
  allocation occur inside one store call.

### Reading

Paged reads (`EventLog.read` and the `TemporalCollection` scans) use the same
loop:

- **Get a batch** of at most 100 entries from the store. The batch starts
  after the last cursor.
- **Resolve each entry through the access object.** Skip the entries that
  raise `AccessDenied` and the entries for blocks that do not exist.
- **Continue until the loop has `limit` visible items,** or until the store
  has no more entries.
- **So a structure limit counts visible items,** as a search limit does.
  The cost is more round trips if most entries are hidden.
- **An exact lookup hides nothing.** If an entry refers to a block that does
  not exist, `_resolve_exact` raises `MemoryNotFound`. It does not return
  `None`, because an entry without its block is an integrity problem, not a
  visibility problem.

### Composition

| Facade | Built from | Notes |
| --- | --- | --- |
| `OwnershipTree` | `OwnershipAccess` (`BlockReader` + `TreeReader` + `put`/`delete`) | No store. The parents already hold the state |
| `PropertyGraph` | `GraphAccess` (`BlockReader` + `LinkReader` + `LinkWriter`) | No store. The links already hold the state |
| `LineageDAG` | `PropertyGraph` + `AcyclicLinkWriter` | Adds one relation, and refuses edges of all other relations |
| `KeyedCollection` | `KeyedStore` | Each binding has the key `(collection_id, address)` |
| `EventLog` | `SequenceStore` | One log for each container block |
| `TemporalCollection` | `TemporalStore`, and `TemporalIndex` if available | Without the index, it scans and matches the entries |
| `VersionedRecords` | `KeyedCollection` + one `EventLog` for each identity | `revision = sequence + 1`, so revisions start at 1 |

- **Graph traversal is breadth-first and always has a limit.** `walk` returns
  simple paths in breadth-first order. It does not repeat a block inside one
  path. It stops at `max_depth` or `limit`. `shortest_path` keeps a set of
  visited blocks and returns the first path that it finds.
- **If an index exists, the facade applies temporal filters 2 times:** one time
  in the index and one time in `_matches` in the facade. The second pass costs
  little. With it, a partly implemented index cannot make a result wider.

## Backend internals

### `InMemoryBackend`

- **Plain dictionaries and one `RLock`:**
  - blocks by id, and a list of child ids for each parent
  - links by id
  - key bindings by `(collection, namespace, key)`
  - log entries and a next-sequence counter for each log
  - temporal entries by `(collection, block)`
  - the set of relations that are already protected as DAGs
- **Each read and write holds the lock.** Each returned block or link is a deep
  copy. So no caller can change the store through a value that the store
  returned.
- **Block identity on update:** check that the replacement has the same
  `type`, `created_at`, `created_by`, and `policy` as the stored block. This
  check runs with and without `expected_version`.
- **Conditional update:** compare the stored version with `expected_version`.
  Then check that the version of the replacement is exactly 1 higher.
- **Conditional delete:** read the subtree again, and compare it with the
  snapshot blocks of the caller: the same ids, versions, and identity fields.
  So an added or removed descendant, or a block that is deleted and created
  again with the same id, fails in the same way as a changed version.
- **Cascade on delete:** delete the children first, with recursion. Then
  remove the block from the parent index. Remove the block, its links, its key
  bindings, its log entries, and its temporal entries.
- **Relation protection:** a protected relation changes nothing. Otherwise,
  adopt the current links of the relation only if they are already acyclic.
- **Acyclic insert:** validate the endpoints. Refuse a relation that is not
  protected. Refuse a self-edge. Then, before the insert, test if the source
  is reachable from the target.
- **`update_block` guards a change of parent:** a parent that does not exist,
  a block as its own parent, and a move under a descendant all raise
  `MemoryConflict`. No
  public `Memory` call changes `parent_id`. So this guard protects callers
  that use the backend directly.

### Shipped adapters

Each adapter maps the same contracts to its own store. Each adapter does the
same tasks:

- **Guard the driver import** and raise `UnsupportedOperation` if the optional
  package is not installed.
- **Record a schema version and check the layout that the adapter finds.** If
  the store does not match, the adapter refuses it and does not write into it.
  `SQLiteBackend` and `PostgresBackend` compare tables, columns, and indexes.
  `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` check their own
  equivalent structures.
- **Serialize the data that has no native type.** The adapter stores values,
  metadata, and the policy as JSON. On read, it builds the policy again as
  `MemoryPolicy` and `AccessRule` objects.
- **Make conditional work atomic** with the mechanism of the store.
  `SQLiteBackend` uses `BEGIN IMMEDIATE` on a WAL database. `PostgresBackend`
  and `Neo4jBackend` also let only one writer through at a time, so the second
  writer waits. `MongoDBBackend` and `RedisBackend` retry the writer that lost.
- **Translate driver errors** into the `BackendError` family
  (`BackendBusy`, `BackendUnavailable`, `BackendReadOnly`,
  `BackendCapacityExceeded`, `BackendCorruption`). Keep the driver error as
  `__cause__`.
- **Document what a retried commit repeats.** Most operations are safe to
  repeat, because a uniqueness check rejects the second attempt.
  `append_log_entry` is not safe to repeat, because it has no uniqueness key.

### `RoutedBackend`

- **`RoutedBackend` owns no records.** It sends each call to the route that
  owns the block.
- **Each instance has a route cache.** It maps each block id that the instance
  found to a route. A cached route counts only while that route still holds
  the block, because another instance can delete the block and store the id
  again in another route.
- **A shared `RouteCatalog` keeps one route for each id across instances.**
  Without one, a put checks that no route holds the id and then writes, and
  only the lock of the instance protects these 2 steps. With a
  `route_catalog`, a put claims the route of the new id in one atomic step,
  and then writes. A delete releases the entries after the route commits.
- **One route for each block, and one route for each subtree.** This lets a
  facade resolve a container to one adapter. An id that the composite finds
  in 2 routes raises `BackendCorruption`, because a retry cannot repair that
  stored state.
- **Links and search can be route-local or shared.** A shared link store does
  not validate endpoints, so `RoutedBackend` checks them before the write.
- **A global search without a shared search index merges the routes.** Each
  route returns one page after the cursor. `RoutedBackend` sorts the pages
  together by raw `score` with `page_results`, and keeps the first `limit`.
  Paging stays exact. But the order has a meaning only if all routes use the
  same score scale, because the composite does not normalize scores.
- **Acyclic links use the same store as the other links.** `RoutedBackend`
  implements `AcyclicLinkStore`. It sends the call to the shared link store.
  If there is no shared link store, it sends the call to the route that owns
  both endpoints. Route-local links never cross routes, so a route-local cycle
  check is exact. Each route stores its own protection. `protect_relation`
  protects the relation in the shared link store, or else in each route.
- **`BlockRouter` does not claim storage.** A composite that implements
  `BlockRouter` states only that it knows which backend owns a block. It still
  advertises only the capabilities that it implements itself.

## Concurrency and failure

| Concern | Where it is handled |
| --- | --- |
| Lost update on a block | `expected_version` compare-and-swap in `BlockStore.update_block` |
| Block deleted and created again with the same id | Identity check in `BlockStore.update_block` and in the `delete_block` snapshot |
| Subtree change during a delete | `expected_blocks` snapshot in `BlockStore.delete_block` |
| 2 writers bind one key | `KeyedStore.bind_key` and `rebind_key` |
| 2 writers append to one log | `SequenceStore.append_log_entry` allocates the sequence |
| 2 edges close a cycle | `AcyclicLinkStore.add_acyclic_link` checks and inserts in one step |
| A plain link closes a cycle while the relation becomes protected | `AcyclicLinkStore.protect_relation` checks the relation and stores the protection in one step |
| Duplicate temporal membership | `TemporalStore.add_temporal_entry` |
| 2 composite instances store one id in 2 routes | `RouteCatalog.claim_route` in the shared `route_catalog` of `RoutedBackend` |

- **`MemoryConflict` means "try again with fresh state".** It is not a bug
  report. It is the refusal that stops stale work.
- **A version does not identify a block.** A block that is deleted and then
  created again with the same id starts again at version 1. So the backend
  also compares the `type`, `created_at`, `created_by`, and `policy`. These
  fields do not change during the life of a block. A write made from the
  old block cannot change the access or the value of the new block.
- **Limit: the clock resolution.** `created_at` comes from the system clock.
  If the old block and the new block get the same `created_at`, and also
  have the same `type`, `created_by`, and `policy`, the check sees one
  block. Access stays the same, because these fields decide access. But a
  stale write can then replace the value of the new block.
- **Hooks are not transactional.** They run synchronously on the thread of the
  caller. Their external side effects are not part of a backend transaction.
  So an `after_*` hook can see work that a later failure does not undo.
- **Genie does not retry an operation by itself.** Genie raises the error, and
  the application decides if it retries. Each adapter documents what its own
  retries repeat.

## Invariants a change must keep

- **One backend write for each public operation,** so no compensation step is
  necessary. `RoutedBackend` with a `route_catalog` is the one composite that
  writes 2 stores for a put: the claim, then the block. If the block write
  fails and the route does not hold the block, it releases the claim. A claim
  that stays without a block only refuses later puts of that id.
- **Authorization uses the stored state,** never an object from the caller.
- **Each policy refusal and each handle refusal goes to `on_denied`.** A
  filtered read of `Memory` or a handle does not report the items that it
  leaves out. A structure facade listing gets each member through the access
  object, so it reports each hidden member. Each started operation closes
  with exactly one terminal event.
- **`Memory` validates each hook return value** before it reaches the
  backend. `before_put` can change only the value, metadata, content type, and
  policy. `before_update` can change only the value, metadata, and content
  type. Link hooks can change only metadata.
- **The policy is immutable** after creation, and a handle only narrows.
- **Atomic behavior is part of the backend contract,** with reusable contract
  and concurrency tests. It is not a read-then-write sequence in a facade.
- **Stores return copies** and return pages in a stable order.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components and boundaries
- [Overview](01-overview.md) — the model and the reasons for it
- [Repository organization](07-repo-organization.md) — the location of each module
- [Backends](../lib/src/genie/backend/README.md) — the full adapter checklist
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Hooks](../lib/src/genie/hooks/README.md)
