# API Interface Spec

This note gives the public Python interface of Genie: each entry point, its
signature, its return value, and the errors that it raises.
[Detailed architecture](04-detailed-architecture.md) tells how the calls work
inside. [Data model](10-data-model.md) gives the records that the calls read and
write.

## A worked example

A billing support service gives a triage agent access to one desk only.

```python
from genie import AccessDenied, InMemoryBackend, KeyedCollection, Memory, MemoryAddress, MemoryPolicy

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="service:support")

desk = memory.put(type="desk", value={"team": "Billing support"}, policy=MemoryPolicy.public())
other = memory.put(type="desk", value={"team": "Sales"})
ticket = memory.put(parent=desk, type="ticket", value={"number": "T-4471", "status": "open"}, policy=MemoryPolicy.public())

agent = memory.get_handle(root=desk)
agent.update(ticket, value={"status": "triaged"})           # version 1 -> 2
agent.search("T-4471", types=["ticket"])                    # only the subtree of the desk

try:
    agent.get(other)
except AccessDenied:
    pass                                                    # outside the handle root

keys = KeyedCollection(agent, backend, collection=desk)
keys.bind(MemoryAddress(("tickets",), "T-4471"), ticket)
keys.get(MemoryAddress(("tickets",), "T-4471")).value     # {"number": "T-4471", "status": "triaged"}
```

| Call | Surface | Result |
| --- | --- | --- |
| **`memory.put(...)`** | `Memory` | a decoded `MemoryBlock` at `version` `1` |
| **`memory.get_handle(root=desk)`** | `Memory` | a `MemoryHandle` bounded to the desk subtree |
| **`agent.update(...)`** | `MemoryHandle` | the same block at `version` `2`, with the merged value |
| **`agent.get(other)`** | `MemoryHandle` | `AccessDenied`, which Genie reports to `on_denied` |
| **`keys.bind(...)`** | `KeyedCollection` | a `KeyedItem`. The `parent_id` of the ticket does not change |

## At a glance

| Surface | Main names | Used by |
| --- | --- | --- |
| **Memory** | `Memory`, `MemoryHandle` | All applications |
| **Records** | `MemoryBlock`, `MemoryLink`, `LinkRelations` | All applications |
| **Query** | `MemoryQuery`, `Scope`, `SearchCursor`, `SearchResult`, `matches_filters`, `normalize_filters`, `page_results` | Search callers and search adapters |
| **Policy** | `MemoryPolicy`, `AccessRule`, `Principal`, `PolicyEngine`, `Operation` | Applications that share blocks |
| **Structures** | `OwnershipTree`, `PropertyGraph`, `LineageDAG`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `VersionedRecords` | Applications that organize blocks |
| **Hooks** | `MemoryHook`, `HookRegistry`, `HookContext`, and their enums | Audit, metrics, and validation |
| **Codecs** | `MemoryCodec`, `CodecRegistry`, `EncodedValue`, `JsonCodec`, `MAX_NESTING_DEPTH`, stdlib codecs | Applications with non-JSON values |
| **Backend contracts** | `MemoryBackend` and the capability protocols | Adapter authors |
| **Adapters** | `InMemoryBackend`, `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, `RedisBackend`, `RoutedBackend` | Application setup code |
| **Errors** | `GenieError` and its subclasses, `AccessDenied` | All callers |

## Conventions

- **Each public name is available from `genie`** and from its subpackage
  (`genie.memory`, `genie.structures`, `genie.backend`, and the other
  subpackages). Both import paths give the same object.
- **Each call is synchronous.** No call returns a future. Hooks run on the
  thread of the caller.
- **Block arguments accept an id or a block.** For parameters of type
  `str | MemoryBlock` (`BlockRef` in the structures), Genie reads only the `id`.
  Authorization always uses the stored block, never the object that the caller
  gives.
- **Options are keyword-only.** Only the main subject of a call is positional:
  a block id, a value, a link, or a query.
- **Returned objects are copies.** Records are frozen dataclasses with
  `slots=True`. Values are decoded and deep-copied, so a change to a result
  never changes stored state.
- **A list call filters. A single-item call raises an error.** `children`,
  `ancestors`, `list_links`, `search`, and the list calls of the structures
  remove the items that the principal cannot read. `get`, `set`, and exact
  lookups raise `AccessDenied` instead.
- **Only `get` gives `None` for a block that does not exist.** `Memory.get`
  and the structure `get` methods return `None` for an unknown key. A call that
  needs a block raises `MemoryNotFound` if the block does not exist.
- **Stale writes raise `MemoryConflict`.** This error tells the caller to read
  again and retry. No call retries automatically.
- **A bad argument type raises `TypeError`. A bad argument value raises
  `ValueError`.** If no registered codec can decode a stored value, Genie also
  raises `TypeError`.
- **Ids, block types, and relations are non-empty strings.** A block argument
  that is not a `MemoryBlock` or a string raises `TypeError`. A `type` or `id`
  in `put` that is not a string also raises `TypeError`. So does a `relation`
  that is not a string. An empty string, or a string of only whitespace, raises
  `ValueError`.
  A bad value never gets to the backend. The tables below do not repeat these
  errors.
- **JSON values and metadata nest at most `MAX_NESTING_DEPTH` (100) levels.**
  Each list or dictionary is one level, and the metadata dictionary is the
  first level. If data is deeper, or if a list or dictionary contains itself,
  Genie raises `ValueError` before the backend runs.

## `Memory`

### Constructor

```python
Memory(*, backend=None, principal=None, policy_engine=None, hooks=None, codecs=None, default_policy=None, attributes=None)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `backend` | `MemoryBackend \| None` | `InMemoryBackend()` | Stores blocks and links, and runs search |
| `principal` | `Principal \| str \| None` | `Principal("system")` | The principal that acts. A string becomes `Principal(id=...)` |
| `policy_engine` | `PolicyEngine \| None` | `PolicyEngine()` | Evaluates `MemoryPolicy` rules |
| `hooks` | `list[MemoryHook] \| HookRegistry \| None` | empty `HookRegistry` | The registry stays the same for the life of the object. Its contents can change |
| `codecs` | `CodecRegistry \| None` | `CodecRegistry([JsonCodec()])` | Encodes and decodes values |
| `default_policy` | `MemoryPolicy \| None` | `MemoryPolicy.owner_private()` | `put` attaches it if the call gives no `policy` |
| `attributes` | `Mapping[str, str] \| None` | `{}` | Genie copies them into each `HookContext` |

- **Raises:** `TypeError` for an argument of the wrong type. `ValueError` for
  an empty principal id or a duplicate hook.
- **`Memory` holds no block data.** 2 `Memory` objects over one backend see the
  same blocks.

### Blocks

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `put(*, type, value, parent=None, metadata=None, policy=None, content_type=None, id=None)` | `MemoryBlock` | `write` and `traverse_down` on the parent | `AccessDenied`, `MemoryConflict` (taken `id`), `MemoryNotFound` (parent), `TypeError`, `ValueError` |
| `get(block_id)` | `MemoryBlock \| None` | `read` | `AccessDenied`, `TypeError` (bad id or no codec), `ValueError` (empty id) |
| `set(block_id, value, *, content_type=None)` | `MemoryBlock` | `write`, then `read` | `AccessDenied`, `MemoryConflict`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `update(block_id, *, value=None, metadata=None)` | `MemoryBlock` | `write`, then `read` | `AccessDenied`, `MemoryConflict`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `delete(block_id)` | `None` | `write` on the target only | `AccessDenied`, `MemoryConflict`, `MemoryNotFound`, `TypeError` (no codec for a block in the subtree) |
| `require_access(operation, block_id)` | `None` | the named operation | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` (empty id or unknown operation) |

- **`put` without `parent` creates a root block.** No parent check runs.
- **`require_access` changes nothing.** It runs the same check as the named
  operation. It reports a refusal to `on_denied` under `access.require`.
  Structure facades call it to check `write` on a container before a membership
  change.
- **`id` is optional.** Without it, Genie generates a `mem_` id. An empty `id`
  raises `ValueError`. Genie does not use a generated id in its place.
- **`set` replaces the value** and can change `content_type`. **`update`
  merges** the new data into a `dict` value and into metadata, and keeps
  `content_type`. `update(value=...)` on a value that is not a `dict` raises
  `TypeError`.
- **Both `set` and `update` increase `version` by `1`.** They fail with
  `MemoryConflict` if another writer changed the block first.
- **Both `set` and `update` also need `read`,** because they return the stored
  block. Genie checks `write` first. An application builds a write-only inbox
  with `put`. `put` needs `write` and `traverse_down` on the container, and
  returns only the new block.
- **`delete` removes the whole subtree.** It also removes each link, key
  binding, log entry, and temporal entry that names a removed block.
- **`metadata` is always a strict JSON `dict`**, independent of the codec that
  the value uses.
- **`ValueError` from a write** has 2 possible causes. An argument value is bad
  (for example an empty id, or a value that nests too deeply). Or a `before_*`
  hook changed a field that the hook cannot change (for example the parent in
  `before_put`).

### Ownership

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `children(block_id)` | `list[MemoryBlock]` | `traverse_down` on the parent, then `read` for each child | `AccessDenied`, `MemoryNotFound` |
| `ancestors(block_id)` | `list[MemoryBlock]`, nearest parent first | `traverse_up` on the block, then `read` for each ancestor | `AccessDenied`, `MemoryNotFound` |
| `contains(root, block_id)` | `bool` | `traverse_down` on the root, `read` on the candidate | `AccessDenied`, `MemoryNotFound` (root) |

- **No call changes `parent_id`.** The parent of a block is fixed at `put`.

### Links and mounts

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `add_link(source, target, relation, *, metadata=None)` | `MemoryLink` | `write` on source, `read` on target | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `add_acyclic_link(source, target, relation, *, metadata=None)` | `MemoryLink` | the same as `add_link` | also `MemoryConflict` (cycle), `UnsupportedOperation` (no `AcyclicLinkStore`) |
| `update_link(link, *, metadata)` | `MemoryLink` | `write` then `read` on source, `read` on target | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `list_links(block_id, *, relation=None, direction="out")` | `list[MemoryLink]` | `read` on the block and on the 2 ends of each link | `AccessDenied`, `MemoryNotFound`, `ValueError` (direction) |
| `add_mount(source, target, *, metadata=None)` | `MemoryLink` with relation `"genie.mount"` | `write` on source, `read` on target | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `list_mounts(source)` | `list[MemoryLink]` | `read` on source | `AccessDenied`, `MemoryNotFound` |
| `discover_mounts(root, *, include_self=True)` | `list[MemoryLink]` | `search` and `traverse_down` on root, `read` for each source | `AccessDenied`, `MemoryNotFound` |
| `resolve_mount(mount)` | `MemoryBlock \| None` | `read` on source. The target read uses `get` | `AccessDenied`, `MemoryNotFound` (source), `ValueError` (not a mount) |

- **`direction`** is `"out"`, `"in"`, or `"both"` (`LinkDirection`).
- **`update_link` merges metadata only.** The source, target, and relation
  never change. It returns the stored link, so it also needs `read` on the
  source.
- **Only `add_mount` creates `"genie.mount"` links.** `add_link` and
  `add_acyclic_link` refuse that relation with `ValueError`. So a handle
  without `allow_mounts` cannot create a mount.
- **Links do not imply ownership.** Recursive delete and subtree search do not
  follow them.

### Search

```python
search(query=None, *, text=None, root=None, scope=Scope.GLOBAL, types=(), filters=None, limit=20)
```

- **3 input forms:** a `MemoryQuery`, a text string as `query`, or keyword
  arguments. Genie changes each form into one `MemoryQuery`.
- **Returns** `list[SearchResult]`. Each result is inside the scope, and the
  principal can `search` and `read` it.
- **`limit` counts visible results.** Hidden results do not use part of it.
  If many matching blocks are hidden, `Memory` calls the backend again. There
  is no cap.
- **Order:** `score` from high to low, then `created_at` from old to new, then
  block id.
- **Next page:** a `MemoryQuery` with `after=results[-1].cursor`.
- **Genie filters before it decodes.** It decodes only the results that the
  policy and scope filters keep. So a hidden block with a content type that
  this `Memory` cannot decode does not change the results.
- **Raises:** `AccessDenied` (the root is not searchable or not traversable),
  `MemoryNotFound` (root), `TypeError` (bad argument, or no codec for a visible
  result), `ValueError` (unknown scope, negative limit, or a non-global scope
  with no root).

### Handles

| Method | Returns | Raises |
| --- | --- | --- |
| `get_handle(*, root, principal=None, default_scope=Scope.SUBTREE, allow_up=False, allow_global=False, allow_mounts=False, allow_policy_override=False, allow_custom_ids=False)` | `MemoryHandle` bounded to `root` | `AccessDenied` (the handle principal cannot `read` the root), `MemoryNotFound`, `TypeError`, `ValueError` |
| `get_unbounded_handle(*, principal=None, default_scope=Scope.SUBTREE, ...same flags)` | `MemoryHandle` with no root | `TypeError`, `ValueError` |

- **`principal=None` keeps the principal of this memory.**
- **Genie checks `default_scope` against the flags.** For example,
  `default_scope="global"` without `allow_global=True` raises `ValueError`.
- **`hooks`** is a read-only property that returns the `HookRegistry`.

## `MemoryHandle`

A `MemoryHandle` is a bounded view of one `Memory`. It has the same block,
ownership, link, mount, and search methods as `Memory`, with these differences:

| Difference | Handle behavior |
| --- | --- |
| **Boundary** | The handle accepts only ids inside the root. It refuses an unknown id as "outside the handle" before any lookup, so it does not show if that block exists |
| **Optional root argument** | `children()`, `ancestors()`, and `discover_mounts()` use the handle root by default. On an unbounded handle, a call without this argument raises `AccessDenied` |
| **Search** | `scope=None` uses `default_scope`. A `root` is accepted only inside the handle. The handle builds the query again, so a prebuilt `MemoryQuery` cannot widen it. The handle checks a query that `before_search` rewrote against the handle again |
| **Explicit policy** | `put(policy=...)` needs `allow_policy_override` |
| **Caller-provided id** | `put(id=...)` needs `allow_custom_ids`. Without it, the handle refuses the id before any lookup. With it, a taken id raises `MemoryConflict`, also when the block is outside the root |
| **`contains` candidate** | The handle checks only `root` against the boundary. A candidate outside the handle is also outside `root`, so it gives `False` |
| **`attributes`** | A read-only property with the hook attributes that this handle reports |

| Flag | `HandleCapability` | What it allows |
| --- | --- | --- |
| rooted handle | `SUBTREE` | No access outside the root |
| `allow_up` | `ANCESTORS` | `ancestors()` and `scope="ancestors"` |
| `allow_global` | `GLOBAL_SEARCH` | `scope="global"` |
| `allow_mounts` | `MOUNTS` | `add_mount`, `list_mounts`, `discover_mounts`, `resolve_mount` |
| `allow_policy_override` | `POLICY_OVERRIDE` | `put(policy=...)` |
| `allow_custom_ids` | `CUSTOM_IDS` | `put(id=...)` |
| unbounded handle | `ROOT_REQUIRED` | Refuses a call that needs a root that the handle cannot infer |
| child handle | `PRINCIPAL` | Refuses a different principal |

- **`get_handle` on a handle only narrows.** The child root is accepted only
  inside the parent root. A flag is accepted only if it is on in the parent. A
  request for wider access, or for a different principal, raises
  `AccessDenied`.
- **A handle refusal is `AccessDenied`.** Genie reports it to `on_denied` with
  the `HandleCapability` that refused the call.
- **Direct construction**
  (`MemoryHandle(memory=..., root_id=..., principal=...)`) is possible.
  `get_handle` is the path with checks.

## Records

| Type | Constructor | Notes |
| --- | --- | --- |
| `MemoryBlock` | `(type, value, id=<mem_...>, parent_id=None, content_type="application/json", metadata={}, policy=None, created_at, updated_at, created_by=None, version=1)` | `with_updates(**changes)` returns a new block |
| `MemoryLink` | `(source_id, target_id, relation, id=<link_...>, metadata={}, created_at)` | Only `metadata` can change |
| `LinkRelations` | `LinkRelations.MOUNT == "genie.mount"` | The only reserved relation |

- **Applications read these records, and rarely build them.** `Memory` fills
  `created_by`, the times, and `version`. Adapter authors build the records
  directly.
- **Field rules** are in [Data model](10-data-model.md#records).

## Query

### `MemoryQuery`

```python
MemoryQuery(text=None, root_id=None, scope=Scope.GLOBAL, types=(), filters=None, limit=20, after=None)
```

- **A `MemoryQuery` is frozen after `__init__`.** The scope becomes a `Scope`,
  `types` becomes a tuple, and the filters are normalized.
- **`limit`:** `0` is permitted. A negative value raises `ValueError`.
- **`after`:** a `SearchCursor` or `None`. Another type raises `TypeError`.
- **The backend decides the meaning of `text`.** `InMemoryBackend` matches a
  case-insensitive substring. A search adapter can rank with its own method.

### `Scope`

| Value | Results come from |
| --- | --- |
| `Scope.SELF` (`"self"`) | the root block only |
| `Scope.SUBTREE` (`"subtree"`) | the root and its descendants |
| `Scope.ANCESTORS` (`"ancestors"`) | the root and its ancestors |
| `Scope.LINKS` (`"links"`) | the root and the blocks linked to it in the 2 directions. A mount target is linked, but its subtree is not |
| `Scope.GLOBAL` (`"global"`) | all blocks. No root is necessary |

### Filters

- **Shape:** `{field: value}` for equality, or `{field: {operator: value}}`.
- **Field lookup:** a `metadata.` prefix reads metadata. A `MemoryBlock` field
  name reads that field. Any other name reads metadata.
- **Operators:** `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`. Genie
  rewrites the aliases `==`, `=`, `!=`, `>`, `>=`, `<`, `<=`, and `in` to these
  operators.
- **A comparison of 2 different types gives `False`,** not an error. An unknown
  operator raises `ValueError` when the query is built.
- **`normalize_filters(filters)`** returns the canonical form.
  **`matches_filters(block, filters)`** is the portable matcher that adapters
  reuse.

### `SearchResult`

- **Fields:** `block` (`MemoryBlock`), `score` (`float`, default `1.0`),
  `reason` (`str | None`).
- **`cursor` property:** the `SearchCursor` directly after this result.
- **The backend decides the meaning of a score.** Genie does not compare
  scores across backends. A score uses only the block and the query, and it
  is the same on each call.

### `SearchCursor`

```python
SearchCursor(score, created_at, block_id)
```

- **A position in the search order.** A search with `after=cursor` returns
  only the results after it.
- **Checks:** `score` is a finite number, `created_at` is a timezone-aware
  `datetime`, and `block_id` is a non-empty string. Otherwise `TypeError` or
  `ValueError`.
- **No access check.** A cursor can name a block that the caller cannot see.

### `page_results`

```python
page_results(results, query)
```

- **Sorts** scored results in the search order, drops the results at or
  before `query.after`, and keeps the first `query.limit`.
- **Each shipped adapter uses it** after it scores its candidates.

## Policy

| Name | Signature | Meaning |
| --- | --- | --- |
| `MemoryPolicy` | `(read, write, search, traverse_up, traverse_down, operations={})` | One `AccessRule` for each built-in operation, plus named extra rules. Each built-in field has the default `AccessRule.owner()` |
| `MemoryPolicy.owner_private()` | classmethod | Only `created_by` can do an operation. This is the default |
| `MemoryPolicy.public()` | classmethod | All principals can do all operations |
| `MemoryPolicy.owner_or(*, principals=None, roles=None)` | classmethod | The owner plus the listed principals and roles |
| `AccessRule` | `(allow_all=False, principals=frozenset(), roles=frozenset(), allow_owner=False)` | One rule. `AccessRule()` denies all principals |
| `AccessRule.allow()`, `.deny()`, `.owner()` | classmethods | All principals, no principal, the owner only |
| `AccessRule.only(*, principals=None, roles=None, owner=False)` | classmethod | An explicit list |
| `AccessRule.allows(principal, *, owner_id=None)` | method | `bool` |
| `Principal` | `(id="system", roles=frozenset(), metadata={})` | The identity that acts |
| `Principal.coerce(value)` | classmethod | Changes a `Principal`, `str`, or `None` to a `Principal` |
| `PolicyEngine` | `(*, operations=None)` | Default rules for named extra operations |
| `PolicyEngine.allows(operation, block, principal)` | method | `bool` |
| `PolicyEngine.require(operation, block, principal)` | method | `None`, or `AccessDenied` |

- **`Operation`** values: `READ`, `WRITE`, `SEARCH`, `TRAVERSE_UP`,
  `TRAVERSE_DOWN`. An `operation` argument also accepts a string name.
- **Defaults fail closed.** `AccessRule()` denies all principals, and
  `MemoryPolicy()` equals `MemoryPolicy.owner_private()`. If a policy sets only
  some fields, the other fields stay owner-only.
- **`allow_all=True` with `principals`, `roles`, or `allow_owner` raises
  `ValueError`,** because with `allow_all` those grants have no effect.
- **An unknown operation name raises `ValueError`.** Genie never allows it
  silently.
- **Extra rules cannot use a built-in name.** Such a name raises `ValueError`
  at construction.
- **The policy of a block is fixed at `put`.** No call changes it.

## Structure facades

- **Each facade takes an access object first:** a `Memory` or a
  `MemoryHandle`. Block reads and writes go through it, so handle and policy
  checks still apply.
- **A membership change needs `write` on the container.** `bind`, `rebind`,
  `unbind`, `append`, `add`, `replace`, and `remove` first call
  `require_access` on the container block. `VersionedRecords.append` checks the
  identity block, which is the anchor of the revision log.
- **Facades that keep records also take a store.** The store is a backend with
  the named capability, or a `BlockRouter`. A `BlockRouter` resolves to the
  route that owns the container block.
- **Construction checks the capability.** The constructor raises
  `UnsupportedOperation` if the store does not have the capability. It also
  resolves the container block through the access object. So a container that
  the caller cannot see raises `MemoryNotFound`.
- **`hooks=None`** uses the registry of the access object.
- **A paged `limit` counts visible items,** as in search. The default is
  `100`. `EventLog.tail` uses `20`.

| Facade | Constructor | Store capability |
| --- | --- | --- |
| `OwnershipTree` | `(access, *, root)` | none |
| `PropertyGraph` | `(access)` | none |
| `LineageDAG` | `(access, *, relation)` | `AcyclicLinkStore`, through the access object |
| `KeyedCollection` | `(access, store, *, collection, hooks=None)` | `KeyedStore` |
| `EventLog` | `(access, store, *, log, hooks=None)` | `SequenceStore` |
| `TemporalCollection` | `(access, store, *, collection, index=None, hooks=None)` | `TemporalStore`, and optionally `TemporalIndex` |
| `VersionedRecords` | `(access, store, *, collection, hooks=None)` | `VersionedStore` (`KeyedStore` + `SequenceStore`) |

### `OwnershipTree`

| Method | Returns |
| --- | --- |
| `root` (property) | `MemoryBlock` |
| `create_child(*, type, value, parent=None, metadata=None, policy=None, content_type=None, id=None)` | `MemoryBlock`. `parent=None` means the tree root |
| `children(block=None)` | `list[MemoryBlock]` |
| `parent(block)` | `MemoryBlock \| None`. `None` at the tree root, or when the principal cannot read the parent |
| `ancestors(block, *, include_self=False)` | `list[MemoryBlock]`, never above the tree root |
| `descendants(block=None, *, include_self=False)` | `list[MemoryBlock]`, depth-first pre-order |
| `contains(block)` | `bool` |
| `delete_subtree(block)` | `None` |

- **A block outside the tree raises `AccessDenied`.**
- **A member check calls `contains` and `get` on the access object.** It
  needs `traverse_down` on the tree root and `read` on the member. The tree
  root itself needs only `read`. The policies of other blocks have no effect
  on the check, and it does not walk the tree.
- **`descendants` skips a closed branch.** A block that denies
  `traverse_down` is in the result, but its descendants are not. A denied
  `traverse_down` on the selected block raises `AccessDenied`.

### `PropertyGraph` and `LineageDAG`

| `PropertyGraph` method | Returns |
| --- | --- |
| `add_edge(source, target, relation, *, metadata=None)` | `MemoryLink` |
| `update_edge(edge, *, metadata)` | `MemoryLink` |
| `edges(block, *, relation=None, direction="out")` | `list[MemoryLink]` |
| `neighbors(block, *, relation=None, direction="out")` | `list[GraphNeighbor]` |
| `walk(start, *, relation=None, direction="out", max_depth=1, limit=100)` | `list[GraphPath]`, breadth-first simple paths |
| `shortest_path(start, target, *, relation=None, direction="out", max_depth=6)` | `GraphPath \| None` |

| `LineageDAG` method | Returns |
| --- | --- |
| `add(predecessor, successor, *, metadata=None)` | `MemoryLink`. `MemoryConflict` for a cycle |
| `update_edge(edge, *, metadata)` | `MemoryLink`. `MemoryConflict` for an edge of a different relation |
| `edges(block, *, direction="out")` | `list[MemoryLink]` |
| `predecessors(block)`, `successors(block)` | `list[GraphNeighbor]` |
| `walk_predecessors(block, *, max_depth=6, limit=100)`, `walk_successors(...)` | `list[GraphPath]` |
| `shortest_path(predecessor, successor, *, max_depth=6)` | `GraphPath \| None` |

- **`LineageDAG` works on one relation.** An empty relation or `"genie.mount"`
  raises `ValueError`.
- **Relation names are shared across the whole backend.** The protection starts
  with the first acyclic insert. After that, it covers each block with that
  relation, across handles and tenants. Applications prefix lineage relations,
  for example `"billing.derived_from"`, to prevent accidental name clashes.
- **`GraphNeighbor`:** `block`, `link`, `direction` (`"out"` or `"in"`).
  **`GraphPath`:** `block_ids`, `links`.

### `KeyedCollection`

| Method | Returns | Raises |
| --- | --- | --- |
| `collection` (property) | `MemoryBlock` | |
| `bind(address, block)` | `KeyedItem` | `MemoryConflict` (address in use), `MemoryNotFound` |
| `rebind(address, block, *, expected)` | `KeyedItem` | `MemoryConflict` (no binding, or not `expected`) |
| `unbind(address, *, expected)` | `None` | `MemoryConflict` |
| `get(address)` | `MemoryBlock \| None` | `MemoryNotFound` (the binding refers to a block that does not exist) |
| `get_item(address)` | `KeyedItem \| None` | `MemoryNotFound` |
| `items(*, namespace=None, recursive=False, limit=100)` | `list[KeyedItem]`, in ascending address order | `TypeError`, `ValueError` |

- **`MemoryAddress(namespace, key)`:** `namespace` is a tuple of non-empty
  strings (`()` is the root). `key` is a non-empty string.
- **`rebind` and `unbind` are compare-and-swap operations** on `expected`.

### `EventLog`

| Method | Returns |
| --- | --- |
| `log` (property) | `MemoryBlock` |
| `append(block)` | `LogItem` with the allocated `sequence` |
| `get(sequence)` | `LogItem \| None` |
| `read(*, start=None, stop=None, reverse=False, limit=100)` | `list[LogItem]`, half-open `[start, stop)` |
| `tail(*, limit=20)` | `list[LogItem]`, newest items in oldest-first order |

- **Sequences start at `0`, and Genie never reuses them.** A deleted block
  leaves a gap.

### `TemporalCollection`

| Method | Returns | Raises |
| --- | --- | --- |
| `collection` (property) | `MemoryBlock` | |
| `add(block, *, occurred_at=None, validity=None)` | `TemporalItem` | `MemoryConflict` (already a member) |
| `replace(block, *, occurred_at=None, validity=None)` | `TemporalItem` | `MemoryConflict` (not a member) |
| `remove(block)` | `None`. The block stays | `MemoryConflict` (not a member) |
| `get(block)` | `TemporalItem \| None` | `MemoryNotFound` (the entry refers to a block that does not exist) |
| `events(*, start=None, stop=None, limit=100)` | `list[TemporalItem]` with `occurred_at` in `[start, stop)` | `TypeError`, `ValueError` (naive time) |
| `valid_at(when, *, limit=100)` | `list[TemporalItem]` | `TypeError`, `ValueError` |
| `overlapping(interval, *, limit=100)` | `list[TemporalItem]` | `TypeError`, `ValueError` |
| `scan(*, occurred_start=None, occurred_stop=None, valid_at=None, overlaps=None, limit=100)` | `list[TemporalItem]` that match all given filters | |

- **`ValidityInterval(valid_at, invalid_at=None)`** is half-open.
  `invalid_at=None` means that the interval has no end.
- **All times are timezone-aware,** and Genie normalizes them to UTC. A naive
  `datetime` raises `ValueError`.
- **`index`:** the facade uses it if the caller gives it. Without it, the
  facade scans the entries and matches them itself.

### `VersionedRecords`

| Method | Returns |
| --- | --- |
| `collection` (property) | `MemoryBlock` |
| `register(address, identity)` | `KeyedItem`. `MemoryConflict` if the address is already registered |
| `identity(address)` | `MemoryBlock \| None` |
| `identities(*, namespace=None, recursive=False, limit=100)` | `list[KeyedItem]` |
| `append(address, block)` | `RevisionItem`. `MemoryNotFound` if no identity exists |
| `current(address)` | `RevisionItem \| None` |
| `revision(address, revision)` | `RevisionItem \| None` |
| `history(address, *, start=1, stop=None, reverse=False, limit=100)` | `list[RevisionItem]`, half-open |

- **Revisions start at `1`.** `revision < 1` raises `ValueError`.
- **Each revision is its own block.** `append` does not change the block that
  the revision refers to.

### Read results

| Type | Fields |
| --- | --- |
| `KeyedItem` | `binding` (`KeyBinding`), `block` |
| `LogItem` | `entry` (`LogEntry`), `block` |
| `TemporalItem` | `entry` (`TemporalEntry`), `block` |
| `RevisionItem` | `entry` (`RevisionEntry`: `record_id`, `revision`, `block_id`), `block` |

### Access protocols

- **Facades declare the narrow access that they need,** as `runtime_checkable`
  protocols. A custom access object needs only those methods.

| Protocol | Methods |
| --- | --- |
| `BlockReader` | `get` |
| `BlockWriter` | `put`, `set`, `update`, `delete` |
| `AccessChecker` | `require_access` |
| `TreeReader` | `children`, `ancestors`, `contains` |
| `LinkReader` | `list_links` |
| `LinkWriter` | `add_link`, `update_link` |
| `AcyclicLinkWriter` | `add_acyclic_link` |
| `SearchAccess` | `search` |
| `OwnershipAccess` | `BlockReader` + `TreeReader` + `put`, `delete` |
| `GraphAccess` | `BlockReader` + `LinkReader` + `LinkWriter` |
| `LineageAccess` | `GraphAccess` + `AcyclicLinkWriter` |
| `KeyedAccess`, `SequenceAccess`, `TemporalAccess`, `VersionedAccess` | `BlockReader` + `AccessChecker` |
| `MemoryAccess` | all of the above. `Memory` and `MemoryHandle` satisfy it |

## Hooks

### `MemoryHook`

- **`MemoryHook` is a base class with no-op callbacks.** A hook overrides only
  the callbacks that it needs.
- **`fail_open: bool = False`.** If `True`, Genie logs and drops a failure in an
  observing callback.

| Callback | Kind | Returns |
| --- | --- | --- |
| `before_put(context, block)` | mutating | `MemoryBlock` |
| `before_update(context, current, proposed)` | mutating | `MemoryBlock` |
| `before_get(context, block_id)` | mutating | `None` |
| `before_search(context, query)` | mutating | `MemoryQuery` |
| `after_search(context, query, results)` | mutating | `list[SearchResult]` |
| `before_traverse(context, traversal)` | mutating | `None` |
| `before_add_link(context, link)` | mutating | `MemoryLink` |
| `before_update_link(context, current, proposed)` | mutating | `MemoryLink` |
| `before_structure_change(context, change)` | mutating | `None` |
| `before_delete(context, block)` | mutating | `None`. Once for each block in the subtree |
| `after_put(context, block)` | observing | `None` |
| `after_update(context, previous, stored)` | observing | `None` |
| `after_get(context, block)` | observing | `None`. `block` can be `None` |
| `after_traverse(context, traversal)` | observing | `None` |
| `after_add_link(context, link)` | observing | `None` |
| `after_update_link(context, previous, stored)` | observing | `None` |
| `after_structure_change(context, change)` | observing | `None` |
| `after_delete(context, block)` | observing | `None`. Once for each removed block |
| `on_denied(context, operation, subject_id, reason)` | observing | `None` |
| `on_suppressed(context, suppression)` | observing | `None` |
| `on_operation_end(context, outcome, error)` | observing | `None`. Exactly once for each operation that starts |

- **Mutating callbacks stop at the first failure,** and the operation raises
  that error. `fail_open` does not apply to them.
- **All observing callbacks run.** Then Genie raises the first failure. If the
  operation already failed, Genie attaches that failure as a note.
- **Genie validates the return value of a `before_*` hook.** A change to a
  protected field raises `ValueError`. A wrong return type raises `TypeError`.
- **One `after_delete` call becomes many calls.** `Memory.delete` calls
  `HookRegistry.after_delete(context, *blocks)` once with all removed blocks.
  The registry then calls `after_delete(context, block)` on each hook once for
  each block. It raises failures only after all hooks received all blocks.
- **Delete hooks get decoded blocks.** So the `Memory` that deletes needs a
  codec for each block in the subtree, also for the blocks that the principal
  cannot read. If a codec is missing, `delete` raises `TypeError` before any
  hook runs, and the backend deletes nothing.

### `HookRegistry`

| Member | Returns | Raises |
| --- | --- | --- |
| `HookRegistry(hooks=None, *, max_depth=16)` | registry | `TypeError`, `ValueError` (duplicate hook, negative depth) |
| `hooks`, `mandatory` (properties) | `tuple` of hooks, in dispatch order | |
| `register(hook, *, mandatory=False)` | `None` | `TypeError`, `ValueError` (already registered) |
| `unregister(hook)` | `None` | `ValueError` (not registered, or mandatory) |
| `scope(name, *, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError` |
| `suppress(*hooks, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError`, or the first `on_suppressed` failure |
| `operation(operation, *, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError` |

- **`scope` groups operations** under one parent context. It dispatches no
  event itself.
- **`suppress()` with no hooks silences the whole registry.** With hooks, it
  silences only those hooks. Genie never silences mandatory hooks.
- **`operation` is for code that extends Genie,** for example a custom facade.
  The dispatch methods (`before_put`, `after_put`, and the others) are for the
  same use.

### Context and event types

| Type | Fields or values |
| --- | --- |
| `HookContext` | `operation_id`, `parent_operation_id`, `operation`, `origin`, `depth`, `principal_id`, `attributes`, `started_at`, `scope_name`, and the `is_scope` property |
| `HookOperation` | `put`, `get`, `set`, `update`, `delete`, `search`, `children`, `ancestors`, `contains`, `link.add`, `link.add_acyclic`, `link.update`, `link.list`, `mount.add`, `mount.list`, `mount.discover`, `mount.resolve`, `handle.create`, `access.require`, `key.bind`, `key.rebind`, `key.unbind`, `log.append`, `temporal.add`, `temporal.replace`, `temporal.remove`, `hooks.suppress`, `scope` |
| `HookOrigin` | `"caller"`, `"hook"` |
| `HookOutcome` | `SUCCESS`, `DENIED`, `ERROR` |
| `Traversal` | `kind`, `origin_id`, `target_id`, `relation`, `direction`, `blocks`, `links`, `contained` |
| `TraversalKind` | `CHILDREN`, `ANCESTORS`, `LINKS`, `CONTAINS`, `MOUNTS`, `SUBTREE_MOUNTS` |
| `StructureChange` | `kind`, `action`, `container_id`, `block_id`, `before`, `after` |
| `StructureKind` | `KEYED_COLLECTION`, `EVENT_LOG`, `TEMPORAL_COLLECTION` |
| `StructureAction` | `BIND`, `REBIND`, `UNBIND`, `APPEND`, `ADD`, `REPLACE`, `REMOVE` |
| `Suppression` | `scope` (`SuppressionScope.SELECTED` or `REGISTRY`), `hooks`, `mandatory` |
| `HandleCapability` | refer to [`MemoryHandle`](#memoryhandle) |

- **`freeze_attributes(attributes)`** validates a mapping from `str` to `str`,
  and returns a read-only copy.

## Codecs

| Name | Signature | Content type |
| --- | --- | --- |
| `MemoryCodec` | protocol: `content_type`, `can_encode(value)`, `encode(value)`, `decode(encoded)` | |
| `EncodedValue` | `(content_type, data)` | |
| `CodecRegistry` | `(codecs=None)`; `register(codec)`, `encode(value, content_type=None)`, `decode(encoded)` | |
| `JsonCodec` | `()` | `application/json` |
| `BytesCodec` | `()` | `application/vnd.genie.bytes+base64` |
| `DatetimeCodec` | `()` | `application/vnd.genie.datetime+json` |
| `DecimalCodec` | `()` | `application/vnd.genie.decimal+json` |
| `UUIDCodec` | `()` | `application/vnd.genie.uuid+json` |
| `DataclassCodec` | `(types=())` | `application/vnd.genie.dataclass+json` |
| `EnumCodec` | `(types=())` | `application/vnd.genie.enum+json` |

- **The default registry holds only `JsonCodec`.** The other codecs are opt-in.
- **Encode selects a codec** by the explicit `content_type`. If no
  `content_type` is given, encode selects the first registered codec whose
  `can_encode` is `True`. **Decode selects** by the stored `content_type`
  only.
- **An unregistered content type raises `TypeError`,** on encode and on decode.
- **`JsonCodec` limits the nesting depth to `MAX_NESTING_DEPTH`.** `can_encode`
  stays `True` for a value that is too deep. So `encode` raises `ValueError`,
  and the registry does not report "no codec". `DataclassCodec` applies the
  same limit to each field.
- **A registry that the application gives to `Memory` replaces the default.**
  If that registry does not include `JsonCodec()`, Genie cannot encode or
  decode JSON values.

## Backend contracts

Adapter authors implement these protocols. Application code rarely calls them.
A direct call skips handles, policies, hooks, and codecs.

| Protocol | Methods |
| --- | --- |
| `BlockStore` | `put_block(block)`, `get_block(block_id)`, `update_block(block, *, expected_version=None)`, `delete_block(block_id, *, expected_blocks=None)` |
| `TraversalIndex` | `children(block_id)`, `ancestors(block_id)`, `descendants(block_id, *, include_self=False)`, `contains(root_id, block_id)` |
| `LinkStore` | `add_link(link)`, `update_link(link)`, `list_links(block_id, *, relation=None, direction="out")`, `delete_links_for_block(block_id)` |
| `SearchIndex` | `search(query)` |
| `MemoryBackend` | `BlockStore` + `TraversalIndex` + `SearchIndex` + `LinkStore` |
| `AcyclicLinkStore` | `add_acyclic_link(link)` |
| `KeyedStore` | `bind_key`, `rebind_key(..., *, expected_block_id)`, `get_key`, `list_keys(..., *, namespace, recursive, after, limit)`, `unbind_key(..., *, expected_block_id)`, `delete_key_bindings_for_block` |
| `SequenceStore` | `append_log_entry(log_id, block_id)`, `get_log_entry`, `list_log_entries(..., *, start, stop, reverse, limit)`, `delete_log_entries_for_block` |
| `TemporalStore` | `add_temporal_entry`, `replace_temporal_entry`, `get_temporal_entry`, `list_temporal_entries(..., *, after_block_id, limit)`, `remove_temporal_entry`, `delete_temporal_entries_for_block` |
| `TemporalIndex` | `query_temporal_entries(collection_id, *, occurred_only, occurred_start, occurred_stop, valid_at, overlaps, after_block_id, limit)` |
| `VersionedStore` | `KeyedStore` + `SequenceStore` |
| `BlockRouter` | `backend_for_block(block_id)` |
| `RouteCatalog` | `claim_route(entry)`, `get_route(block_id)`, `release_routes(block_ids)` |

- **Each protocol is `runtime_checkable`.** Genie checks capabilities with
  `isinstance`. So an adapter claims a capability when it implements the
  methods of that capability.
- **`Memory` needs only `MemoryBackend`.** The other protocols are optional. A
  facade that needs one of them raises `UnsupportedOperation` if the store does
  not have it.
- **Conditional and uniqueness checks occur inside one store call:**
  `expected_version`, `expected_blocks`, the block identity check of
  `update_block`, `expected_block_id`, address uniqueness, cycle checks,
  sequence allocation, temporal membership, and route catalog claims. A
  failed check raises `MemoryConflict`.
- **`delete_block` cascades** to links, key bindings, log entries, and temporal
  entries in the same atomic step.
- **`search` returns one page in the search order:** at most `query.limit`
  results after `query.after`.
- **The shared contract tests** in
  `lib/tests/backends/test_backend_contracts.py` define the expected behavior.
  Refer to [Testing strategy](11-testing-strategy.md).

## Adapters

| Class | Constructor | Extra |
| --- | --- | --- |
| `InMemoryBackend` | `()` | none |
| `SQLiteBackend` | `(path=":memory:", *, timeout=5.0, durability="full")` | none |
| `PostgresBackend` | `(conninfo, *, schema="genie", timeout=5.0, connect_timeout=30.0, durability="full", verify_server_role=True, initialize_schema=True)` | `postgres` |
| `Neo4jBackend` | `(uri, *, auth=None, database="neo4j", connection_timeout=30.0, max_transaction_retry_time=30.0, encrypted=None, verify_connectivity=True, initialize_schema=True)` | `neo4j` |
| `MongoDBBackend` | `(uri, *, auth=None, database="genie", connect_timeout=30.0, socket_timeout=30.0, max_retry_time=30.0, durability="full", verify_server_role=True, verify_transaction_support=True, initialize_schema=True)` | `mongodb` |
| `RedisBackend` | `(url, *, auth=None, key_prefix="genie", connect_timeout=10.0, socket_timeout=30.0, max_retry_time=30.0, health_check_interval=30.0, transport_retries=3, verify_connectivity=True, verify_server_role=True, verify_eviction_policy=True, initialize_schema=True)` | `redis` |
| `RoutedBackend` | `(routes, *, route_selector=None, default_route=None, traversal_index=None, search_index=None, link_store=None, route_catalog=None)` | none |
| `InMemoryLinkStore` | `()` | none |

- **Each storage adapter implements all optional capabilities:**
  `AcyclicLinkStore`, `KeyedStore`, `SequenceStore`, `TemporalStore`,
  `TemporalIndex`, and `RouteCatalog`. `RoutedBackend` implements
  `BlockRouter` and `AcyclicLinkStore`. It sends each acyclic link to the
  store that holds its links. `InMemoryLinkStore` is a standalone
  `LinkStore`.
- **If a driver is not installed, the constructor raises
  `UnsupportedOperation`.** The error names the extra to install (for example
  `genie[postgres]`).
- **Each `verify_*` flag controls one server check at construction.** `False`
  disables that check. **`initialize_schema=False`** uses the current layout and
  does not create it.
- **`RoutedBackend`:** `routes` maps a `RouteKey` (`str`) to a backend.
  `route_selector` is a `RouteSelector`, a callable from `MemoryBlock` to
  `RouteKey | None`. Extra members: `routes`, `register_route(route, backend,
  *, replace=False, make_default=False)`, `backend_for_block(block_id)`,
  `route_for_block(block_id)`. `RouteInfo(block_id, route)` is the entry of a
  `RouteCatalog`.
- **`route_catalog`:** a `RouteCatalog` that all `RoutedBackend` instances over
  the same routes share. A put claims the route of a new id in it, in one
  atomic step, so 2 instances cannot store one id in 2 routes. Without one,
  ids stay unique across routes only for writers that share one instance.
- **Adapter options, schema, and atomicity notes** are in the README of each
  adapter under `lib/src/genie/backend/`.

## Errors

| Error | Base | Raised when |
| --- | --- | --- |
| `GenieError` | `Exception` | Base class of all Genie errors except `AccessDenied` |
| `AccessDenied` | `PermissionError` | A policy or a handle boundary refuses the call |
| `MemoryNotFound` | `GenieError` | A block, link, or root that the call needs does not exist, or a structure record refers to a block that does not exist |
| `MemoryConflict` | `GenieError` | A version, snapshot, key, cycle, or membership check fails |
| `UnsupportedOperation` | `GenieError` | A backend does not have a capability, an adapter driver is not installed, or a `RoutedBackend` does not have the route that its route selector or its `route_catalog` names for a block |
| `HookRecursionError` | `GenieError` | Operations that hooks start nest deeper than `max_depth` |
| `BackendError` | `GenieError` | Base class of the adapter failures below |
| `BackendBusy` | `BackendError` | The store is locked or overloaded. A retry can succeed |
| `BackendUnavailable` | `BackendError` | The store cannot be reached |
| `BackendReadOnly` | `BackendError` | The store refuses writes, for example a replica |
| `BackendCapacityExceeded` | `BackendError` | The store has no more space or memory |
| `BackendCorruption` | `BackendError` | Genie cannot read the stored data or layout safely |

- **`AccessDenied` is a `PermissionError`,** so `except GenieError` does not
  catch it. A caller catches it by its name.
- **Adapters keep the driver error as `__cause__`** on each `BackendError`.

## Left out on purpose

- **No async API.** Each call is synchronous.
- **No batch or transaction API.** Each public call is one backend write.
- **No reparent operation and no policy change.** `parent_id` and `policy` are
  fixed at `put`.
- **No server, CLI, or network protocol.** Genie is a library that the
  application imports.
- **No block types, relation names, or value meanings** other than
  `"genie.mount"`.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components and extension points
- [Detailed architecture](04-detailed-architecture.md) — check order, hook dispatch, and search pipeline
- [Data model](10-data-model.md) — record fields and integrity rules
- [Testing strategy](11-testing-strategy.md) — contract suites
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md)
