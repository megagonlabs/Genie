# Detailed Architecture

How each module works inside: the stored shapes, the order of checks, the
algorithms, and the invariants a change must keep. The components and their
boundaries are in [High-level architecture](03-high-level-architecture.md).

## Stored shapes

### `MemoryBlock`

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `id` | `str` | Caller or `new_memory_id()` | `mem_` plus a `uuid4` hex |
| `type` | `str` | Application | Never interpreted by Genie |
| `value` | `Any` | Codec | Encoded in storage, decoded on the way out |
| `parent_id` | `str \| None` | Caller at creation | The only ownership field |
| `content_type` | `str` | Codec | Selects the codec that decodes `value` |
| `metadata` | `dict` | Caller | Always strict JSON, whatever the value codec is |
| `policy` | `MemoryPolicy \| None` | Creation only | `None` is read as `owner_private()` |
| `created_at`, `updated_at` | `datetime` | `Memory` | Timezone-aware UTC |
| `created_by` | `str \| None` | `Memory` | The principal id that owned the `put`; this is the "owner" in `AccessRule` |
| `version` | `int` | `Memory` | Starts at 1 and rises by exactly 1 per update |

- **Every model is a frozen dataclass with `slots=True`.** A change produces a
  new object through `MemoryBlock.with_updates(...)` or `dataclasses.replace`,
  so nothing that has already been handed out can be edited in place.
- **`with_updates(metadata=None)` writes `{}`,** not `None`, so metadata is
  never absent.
- **Ids are prefixed by kind:** `mem_` for blocks, `link_` for links,
  `handle_` for handles, `hookop_` for hook contexts. The prefix makes a
  stray id readable in a log.

### `MemoryLink`

- **Fields:** `source_id`, `target_id`, `relation`, `id`, `metadata`,
  `created_at`. There is no version and no update timestamp.
- **Only `metadata` is mutable,** and only by merge. `Memory.update_link`
  rebuilds the link with merged metadata and the backend rejects a change to
  any other field.
- **`LinkRelations.MOUNT` (`"genie.mount"`) is reserved.** It marks a mount
  and is refused by `add_acyclic_link`.

## The `Memory` operation template

Every public method on `Memory` follows the same skeleton:

```text
with self._operation(HookOperation.X) as context:   # 1 open context
    block = self._require_raw(id)                   # 2 read encoded state
    self._require_access(context, OP, block)        # 3 authorize, report refusal
    payload = self.codecs.encode(...)               # 4 translate (writes only)
    payload = self.hooks.before_x(context, payload) # 5 let hooks rewrite
    _require_..._invariants(proposed, payload)      # 6 check what hooks changed
    stored = self.backend.write_or_read(payload)    # 7 one backend call
    self.hooks.after_x(context, stored)             # 8 report the outcome
    return self._decode_block(stored)               # 9 translate back
```

- **`_require_raw` reads the encoded block** straight from the backend and
  raises `MemoryNotFound`. Authorization always runs against stored state, not
  against a block object the caller passed in.
- **`_require_access` reports before it raises.** A refusal goes to
  `on_denied` with the operation, the block id, and the message, and then the
  `AccessDenied` leaves the operation, which makes its terminal event
  `HookOutcome.DENIED`.
- **Exactly one backend write per operation.** There is no compensating second
  write to undo, which is why hook rewrites are validated before the write and
  not after.

### Authorization per operation

| Operation | Checked on | Required |
| --- | --- | --- |
| `put` | parent block, when there is one | `write` and `traverse_down` |
| `get` | the block | `read` |
| `set`, `update` | the block | `write` |
| `delete` | the target block only | `write` |
| `children` | the parent | `traverse_down`, then `read` filters each child |
| `ancestors` | the block | `traverse_up`, then `read` filters each ancestor |
| `contains` | the root | `traverse_down`, plus `read` on the candidate |
| `add_link`, `add_acyclic_link`, `add_mount` | both endpoints | `write` on source, `read` on target |
| `update_link` | both endpoints | `write` on source, `read` on target |
| `list_links`, `list_mounts` | the block | `read`, plus `read` on both endpoints of each link |
| `discover_mounts` | the subtree root | `search` and `traverse_down`, plus `read` per source block |
| `resolve_mount` | the mount source | `read`, then the target goes through `get` |
| `search` | the query root | `search`, plus the scope's traversal right |
| `get_handle` | the handle root | `read`, **as the new handle's principal** |

- **A refused list is a shorter list.** `children`, `ancestors`, `list_links`,
  and search drop what the principal cannot read instead of raising.
- **A refused single-block call raises.** `get` separates the two cases: a
  missing block returns `None` before any policy check runs, and a block that
  exists but is unreadable raises `AccessDenied`.
- **`delete` is authorized on the target alone.** Descendant policies are
  never consulted, because write access to a container is already authority
  over what it contains.

### Deletion

1. **Read the target** and require `write`.
2. **Ask the traversal index** for `descendants(id, include_self=True)`.
3. **Dispatch `before_delete` once per block** in that snapshot.
4. **Call `delete_block(id, expected_versions={id: version, ...})`** with the
   exact snapshot, so the adapter fails with `MemoryConflict` if the subtree
   gained, lost, or changed a block in between.
5. **Dispatch `after_delete(*blocks)`** with every removed block in one call.

- **`Memory` never cleans up dependent state.** Links, key bindings, log
  entries, and temporal entries for the removed blocks are the adapter's job
  inside `delete_block`, and must land atomically with it.
- **The traversal index defines the subtree.** An index that omits a
  descendant produces an incomplete snapshot, which a correct `BlockStore`
  rejects.

### Update paths

- **`set` replaces the value.** It re-encodes with the caller's
  `content_type`, or infers a codec again, so `set` can change a block's
  content type.
- **`update` merges.** It decodes the stored value, requires a `dict` when
  `value=` is given, merges both `value` and `metadata`, and re-encodes with
  the block's existing content type.
- **Both bump `version` by one** and pass `expected_version=block.version` to
  the backend, so a concurrent writer loses with `MemoryConflict`.

## Codec boundary

- **Dispatch on encode:** an explicit `content_type` selects a registered
  codec directly, and a missing registration is a `TypeError`. Without one,
  the registry walks its codecs in registration order and takes the first
  whose `can_encode` returns `True`.
- **Dispatch on decode:** by `EncodedValue.content_type` only. A block stored
  by a codec that is no longer registered raises `TypeError`, which is why the
  registry configuration belongs with the store it reads.
- **`JsonCodec` is the default registration** and accepts `None`, `str`,
  `bool`, `int`, finite `float`, `list`, and `dict` with string keys. It
  rejects `Enum` values so `EnumCodec` can claim them.
- **Codec results are validated.** A codec that returns something other than
  an `EncodedValue`, or an `EncodedValue` with a different content type, is a
  `TypeError` or `ValueError` rather than a silently mistyped block.
- **Metadata is not codec territory.** `_copy_metadata` always validates and
  copies metadata through `JsonCodec`, whatever codec the value uses, so
  filters and adapter indexes can rely on strict JSON.
- **Both directions deep-copy.** A caller cannot reach stored state through a
  returned value, and a stored value cannot be changed by mutating what was
  passed in.
- **`genie.codecs.stdlib` adds opt-in codecs** for `bytes` (base64),
  `datetime`, `Decimal`, `UUID`, dataclasses, and enums, each with its own
  `application/vnd.genie.*` content type. None of them is registered by
  default.

## Policy evaluation

`PolicyEngine.allows(operation, block, principal)` resolves in this order:

1. **Type checks** on block, principal, and policy.
2. **Policy resolution:** `block.policy`, or `MemoryPolicy.owner_private()`
   when the block carries none.
3. **Rule lookup:**
   - a built-in name (`read`, `write`, `search`, `traverse_up`,
     `traverse_down`) reads the matching `MemoryPolicy` field;
   - otherwise the block's own `policy.operations[name]`;
   - otherwise the engine's `PolicyEngine(operations=...)` default;
   - otherwise `ValueError`, so a typo is never a silent allow.
4. **`AccessRule.allows`:** `allow_all` wins first, then owner when
   `allow_owner` and `principal.id == block.created_by`, then an explicit
   principal id, then any intersection of roles.

- **`require` is `allows` plus a raise.** Every filtered read path calls
  `allows` directly so it can drop a block instead of failing.
- **Custom operation names cannot shadow built-ins.** `_freeze_operation_rules`
  rejects that at construction, on both the policy and the engine.
- **Rules and policies are frozen and copied.** Sets become `frozenset`,
  operation maps become a read-only mapping, and principal metadata is
  deep-copied, so a policy cannot change after a block carries it.

## `MemoryHandle` internals

- **A handle holds no state of its own** beyond its boundary: the `Memory`, a
  root id, a principal, a default scope, four capability flags, and a frozen
  attribute map.
- **`_as_principal()` builds a derived `Memory`** that shares the backend,
  policy engine, hook registry, codec registry, and default policy, but acts
  as the handle's principal and carries the handle's attributes. Every handle
  method does its own boundary check and then calls that derived object. This
  is why work done through a handle appears in the trail with the handle's id
  and capabilities attached.
- **Check order is boundary first, policy second.** `_require_in_subtree` uses
  `backend.contains(root_id, block_id)` with no policy evaluation, so in a
  rooted handle an unknown id is refused as "outside the handle" before any
  lookup could reveal that it exists.
- **`_deny` opens its own operation.** A handle refusal happens before the
  underlying `Memory` operation starts, so the handle opens one context, emits
  `on_denied` with the `HandleCapability` that refused, and lets the terminal
  event close it. If the registry cannot report it, the reason is attached to
  the `AccessDenied` as a note and the caller still gets the refusal.
- **Search is narrowed twice.** The handle rebuilds the query with its own
  root and scope, so a prebuilt `MemoryQuery` cannot smuggle a wider one, and
  then filters the returned results by `contains` again for every scope except
  `global` and `ancestors`.

| Handle capability | Flag | What it gates |
| --- | --- | --- |
| `SUBTREE` | always on when rooted | Every id must be inside the root |
| `ANCESTORS` | `allow_up` | `ancestors()` and `scope="ancestors"` |
| `GLOBAL_SEARCH` | `allow_global` | `scope="global"` |
| `MOUNTS` | `allow_mounts` | Creating, listing, and resolving mounts |
| `POLICY_OVERRIDE` | `allow_policy_override` | Passing an explicit `policy` to `put` |
| `ROOT_REQUIRED` | unbounded handles | Refuses calls that would need a root to be inferred |
| `PRINCIPAL` | child handles | Refuses a change of principal |

- **`get_handle` narrows only.** Each flag the child asks for must already be
  on the parent, the root must be inside the parent's root, and a different
  principal is refused outright.
- **`default_scope` is validated against the flags** at construction, so a
  handle cannot be created in a state where its own default is refused.

## Hook registry internals

### Context

- **Four `ContextVar` values carry the state:** the current context, the set of
  active `(hook, callback)` pairs, suppressed registries, and suppressed
  `(registry, hook)` pairs. They are context-local, so one thread or task does
  not see another's window.
- **`HookContext` is a node in a tree:** `operation_id`, `parent_operation_id`,
  `operation`, `origin` (`"caller"` or `"hook"`), `depth`, `principal_id`,
  inherited `attributes`, and `scope_name`.
- **Attributes merge downward,** with the inner ones winning. A handle's
  boundary attributes beat the memory's, and an added attribute beats an
  inherited one.
- **`scope()` groups without dispatching.** It opens a context under
  `HookOperation.SCOPE` that no callback ever receives; operations inside it
  report it as their parent and inherit its attributes.
- **`max_depth` only limits hook-started work.** `origin` is `"hook"` when any
  callback is on the stack, and only then does exceeding `max_depth` raise
  `HookRecursionError`. That operation never starts, so it emits no terminal
  event.

### Dispatch

| Kind | Callbacks | Behavior on failure |
| --- | --- | --- |
| Mutating, fail-fast | every `before_*`, `after_search` | Stop at the first failure; `fail_open` is ignored |
| Observing, isolated | `after_put`, `after_update`, `after_get`, `after_traverse`, `after_add_link`, `after_update_link`, `after_structure_change`, `after_delete`, `on_denied`, `on_suppressed`, `on_operation_end` | Run every hook, then report |

- **Mutating callbacks chain.** Each hook receives the previous hook's return
  value, and the return type is checked (`MemoryBlock`, `MemoryQuery`,
  `MemoryLink`, or a list of `SearchResult`).
- **Isolated callbacks report work that already happened,** so stopping early
  would only cost the remaining hooks their record. Failures are collected and
  then: attached as notes when an exception is already leaving the operation,
  or raised (first failure, the rest as notes) when the operation succeeded.
- **`fail_open` hooks are logged and dropped** in the isolated path only.
- **Reentrancy is skipped, not refused.** `_invoke` keys on
  `(id(hook), callback_name)`; a hook whose own callback re-enters the same
  callback gets `_SKIPPED`, and for a mutating callback that means the value
  passes through unchanged.
- **The dispatch order is snapshotted per event.** A hook that registers or
  unregisters during a callback changes the next event, not the one in flight.
- **Registration is by object identity** and a repeat is rejected, so one hook
  cannot report a single operation twice.

### Suppression

- **A window is itself an operation** (`HookOperation.SUPPRESS`), so the trail
  shows the gap instead of just missing events.
- **Order:** plan the window, open the operation, dispatch `on_suppressed` to
  every hook including the ones about to go quiet, then disable dispatch. A
  failure in `on_suppressed` refuses the window and the body never runs.
- **Two scopes:** `REGISTRY` silences everything non-mandatory, including
  hooks registered while the window is open; `SELECTED` silences named hooks,
  and naming an unregistered or mandatory hook is a `ValueError`.
- **Mandatory hooks never stop.** They witness the window, its contents, and
  its terminal event, and they cannot be unregistered.

## Search pipeline

1. **Coerce** a string, keyword arguments, or a `MemoryQuery` into one
   normalized `MemoryQuery`.
2. **Authorize the root:** `search`, plus `traverse_down` for `subtree`,
   `traverse_up` for `ancestors`, and `read` for `links`. A non-global scope
   with no root is a `ValueError`.
3. **`before_search`**, then normalize filters again and **authorize again**,
   because a hook may have returned a freshly built query.
4. **`backend.search(query)`**, which resolves the scope, matches, and applies
   `query.limit`.
5. **Decode** each result block.
6. **Filter** with `_filter_visible_results`.
7. **`after_search`**, then **filter again** with the same verdict cache.

`_can_return_search_result` re-reads the stored block by id and requires
`search`, then `read`, then scope membership:

| Scope | Membership test |
| --- | --- |
| `global` | Always true |
| `self` | `block_id == root_id` |
| `subtree` | `backend.contains(root_id, block_id)` |
| `ancestors` | `block_id == root_id` or `backend.contains(block_id, root_id)` |
| `links` | `block_id == root_id`, or a stored link in either direction joins them |

- **One verdict per block id per call.** Both filter passes share a dict, so a
  block is read once. A verdict depends only on the id and the stored block,
  never on the rest of the result, so reusing it is as strict as asking again.
- **One consequence:** a hook that deletes a block inside `after_search` does
  not remove that result from the page it is already on.
- **`limit` is a candidate budget.** The backend spends it before policy and
  scope filtering, so a visible page can be shorter. A caller that needs a
  full page asks for more and truncates.

## Query normalization

- **`MemoryQuery` normalizes in `__init__`** and is frozen afterwards: scope
  becomes a `Scope`, types become a tuple, filters are rewritten, and a
  negative limit is a `ValueError` while `0` is allowed.
- **Operator aliases are rewritten** to the canonical names adapters see:

| Alias | Canonical |
| --- | --- |
| `==`, `=` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

- **`matches_filters` resolves a field name in three steps:** a `metadata.`
  prefix reads metadata, then a real `MemoryBlock` field name reads the field,
  and anything else falls back to metadata.
- **An incompatible comparison is `False`, not an error.** A `TypeError` from
  comparing mismatched types is caught, and an unknown operator or an empty
  operator map fails the match. A filter can never match by accident.
- **This matcher is the portable floor.** `InMemoryBackend` and the shipped
  adapters use it for the subset they do not push into their own query
  language; a richer adapter may implement more, as long as the same query
  keeps its meaning.

## Structure facade internals

### Construction

1. **Resolve the store.** `resolve_store` returns the store unchanged unless
   it is a `BlockRouter`, in which case the facade binds to the backend that
   owns the container block. A container and its subtree share one route.
2. **Check the capability.** `isinstance` against the `runtime_checkable`
   protocol, raising `UnsupportedOperation` when it is missing. This happens
   at construction, not on first use.
3. **Resolve the container through the access object,** so an invisible
   container raises `MemoryNotFound` for a caller that cannot see it.
4. **Resolve hooks:** the explicit registry, or `access.hooks`, or the
   registry behind a handle's `_memory`. A facade with no registry runs
   through `nullcontext` and dispatches nothing.

### Writing

- **Every mutation opens its own hook operation** (`key.bind`, `key.rebind`,
  `key.unbind`, `log.append`, `temporal.add`, `temporal.replace`,
  `temporal.remove`) and reports a `StructureChange` with `kind`, `action`,
  `container_id`, `block_id`, `before`, and `after`.
- **`after_structure_change` carries the stored record,** not the proposed
  one, so the trail shows the allocated sequence or the persisted binding.
- **The atomic step is the backend's.** The facade prepares and reports; the
  uniqueness check, the compare-and-swap, and the sequence allocation happen
  inside one store call.

### Reading

Paged readers (`EventLog.read`, `TemporalCollection` scans) share one loop:

- **Fetch a batch** of at most 100 entries from the store, starting after the
  last cursor.
- **Resolve each entry through the access object,** skipping `AccessDenied`
  and missing blocks.
- **Keep going until `limit` visible items** are collected or the store runs
  out.
- **So a structure limit counts visible items,** unlike a search limit, which
  counts candidates. The cost is more round trips when most entries are
  hidden.
- **An exact lookup does not hide anything.** `_resolve_exact` raises
  `MemoryNotFound` when an entry points at a block that is gone, instead of
  returning `None`, because that is an integrity problem rather than a
  visibility one.

### Composition

| Facade | Built from | Notes |
| --- | --- | --- |
| `OwnershipTree` | `OwnershipAccess` (`BlockReader` + `TreeReader` + `put`/`delete`) | No store; parents already carry the state |
| `PropertyGraph` | `GraphAccess` (`BlockReader` + `LinkReader` + `LinkWriter`) | No store; links already carry the state |
| `LineageDAG` | `PropertyGraph` + `AcyclicLinkWriter` | Adds one relation and refuses edges of any other |
| `KeyedCollection` | `KeyedStore` | Bindings keyed by `(collection_id, address)` |
| `EventLog` | `SequenceStore` | One log per container block |
| `TemporalCollection` | `TemporalStore`, plus `TemporalIndex` when available | Falls back to scan-and-match without the index |
| `VersionedRecords` | `KeyedCollection` + one `EventLog` per identity | `revision = sequence + 1`, so revisions start at 1 |

- **Graph traversal is breadth-first and always bounded.** `walk` returns
  simple paths in breadth-first order, refuses to repeat a block inside one
  path, and stops at `max_depth` or `limit`. `shortest_path` keeps a visited
  set and returns the first path it reaches.
- **Temporal filters are applied twice** when an index exists: once by the
  index and once by `_matches` in the facade. The second pass costs little and
  keeps a partly implemented index from widening a result.

## Backend internals

### `InMemoryBackend`

- **Plain dictionaries plus one `RLock`:** blocks by id, a child-id list per
  parent, links by id, key bindings by `(collection, namespace, key)`, log
  entries and a next-sequence counter per log, temporal entries by
  `(collection, block)`, and the set of relations already protected as DAGs.
- **Every read and write holds the lock,** and every returned block or link is
  a deep copy, so the store cannot be changed through a value it handed out.
- **Conditional update:** compare the stored version against
  `expected_version`, then require the replacement to be exactly one higher.
- **Conditional delete:** rebuild `{id: version}` for the subtree and compare
  it with the caller's snapshot as a whole, so an added or removed descendant
  fails the same way a changed version does.
- **Cascade on delete:** recurse into children first, then unlink from the
  parent index and remove the block, its links, its key bindings, its log
  entries, and its temporal entries.
- **Acyclic insert:** validate endpoints, adopt the relation only if it is
  already acyclic on first use, refuse a self-edge, then test reachability
  from target back to source before storing.
- **Reparenting is guarded** in `update_block`: a missing parent, a self
  parent, and a move under a descendant are all `MemoryConflict`. No public
  `Memory` call changes `parent_id`, so this guard protects direct backend
  callers.

### Shipped adapters

Each adapter maps the same contracts onto its own store, with the same duties:

- **Guard the driver import** and raise `UnsupportedOperation` when the
  optional package is missing.
- **Record a schema version and check the layout it finds,** refusing a store
  that does not match instead of writing into it. `SQLiteBackend` and
  `PostgresBackend` compare tables, columns, and indexes; `Neo4jBackend`,
  `MongoDBBackend`, and `RedisBackend` check their own equivalents.
- **Serialize what has no native type.** Values, metadata, and the policy are
  stored as JSON and rebuilt into `MemoryPolicy` and `AccessRule` objects on
  read.
- **Make conditional work atomic** with the mechanism the store provides.
  `SQLiteBackend` uses `BEGIN IMMEDIATE` on a WAL database, and
  `PostgresBackend` and `Neo4jBackend` also let one writer at a time through,
  so the second waits. `MongoDBBackend` and `RedisBackend` instead retry the
  writer that lost.
- **Translate driver errors** into the `BackendError` family
  (`BackendBusy`, `BackendUnavailable`, `BackendReadOnly`,
  `BackendCapacityExceeded`, `BackendCorruption`), keeping the driver error as
  `__cause__`.
- **State what a retried commit repeats.** Most operations are safe to repeat
  because a uniqueness check rejects the second one; `append_log_entry` is
  not, because it has no uniqueness key.

### `RoutedBackend`

- **Owns no records.** It keeps a catalog of block id to route, and forwards
  every call to the route that owns the block.
- **One route per block, one route per subtree,** which is what lets a facade
  resolve a container to a single adapter.
- **Links and search may be route-local or shared.** A shared link store does
  not validate endpoints, so `RoutedBackend` checks them before writing.
- **`BlockRouter` is not a storage claim.** Implementing it says only that the
  composite knows who owns a block; it still advertises only the capabilities
  it implements itself.

## Concurrency and failure

| Concern | Where it is handled |
| --- | --- |
| Lost update on a block | `expected_version` compare-and-swap in `BlockStore.update_block` |
| Subtree changed under a delete | `expected_versions` snapshot in `BlockStore.delete_block` |
| Two writers binding one key | `KeyedStore.bind_key` and `rebind_key` |
| Two appenders to one log | `SequenceStore.append_log_entry` allocates the sequence |
| Two edges closing a cycle | `AcyclicLinkStore.add_acyclic_link` checks and inserts together |
| Duplicate temporal membership | `TemporalStore.add_temporal_entry` |

- **`MemoryConflict` means "try again with fresh state".** It is not a bug
  report; it is the refusal that keeps stale work out.
- **Hooks are not transactional.** They run synchronously on the calling
  thread, and their external side effects are not part of any backend
  transaction. An `after_*` hook can therefore observe work that a later
  failure does not undo.
- **Nothing retries by itself.** Genie raises; the application decides whether
  to retry, and adapters document what a retry of theirs repeats.

## Invariants a change must keep

- **One backend write per public operation,** so there is nothing to
  compensate.
- **Authorize stored state,** never a caller-supplied object.
- **Report every refusal** through `on_denied`, and close every started
  operation with exactly one terminal event.
- **Validate whatever a hook returns** before it reaches the backend:
  `before_put` may not change `parent_id`, `before_update` may change only
  value, metadata, and content type, and link hooks may change only metadata.
- **Keep the policy immutable** after creation, and keep handles narrowing
  only.
- **Put atomicity in the backend contract,** with reusable contract and
  concurrency tests, not in a facade's read-then-write.
- **Return copies** from stores, and page in a stable order.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components and boundaries
- [Overview](01-overview.md) — the model and the reasoning behind it
- [Repository organization](07-repo-organization.md) — where each module lives
- [Backends](../lib/src/genie/backend/README.md) — the adapter checklist in full
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Hooks](../lib/src/genie/hooks/README.md)
