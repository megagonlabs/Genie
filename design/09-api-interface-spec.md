# API Interface Spec

The public Python interface of Genie: every entry point, its signature, what it
returns, and what it raises. How the calls work inside is in
[Detailed architecture](04-detailed-architecture.md). The records they read and
write are in [Data model](10-data-model.md).

## A worked example

A billing support service gives a triage agent access to one desk and nothing
else.

```python
from genie import AccessDenied, InMemoryBackend, KeyedCollection, Memory, MemoryAddress, MemoryPolicy

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="service:support")

desk = memory.put(type="desk", value={"team": "Billing support"}, policy=MemoryPolicy.public())
other = memory.put(type="desk", value={"team": "Sales"})
ticket = memory.put(parent=desk, type="ticket", value={"number": "T-4471", "status": "open"}, policy=MemoryPolicy.public())

agent = memory.get_handle(root=desk)
agent.update(ticket, value={"status": "triaged"})           # version 1 -> 2
agent.search("T-4471", types=["ticket"])                    # subtree of the desk only

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
| **`agent.update(...)`** | `MemoryHandle` | the same block at `version` `2`, with the value merged |
| **`agent.get(other)`** | `MemoryHandle` | `AccessDenied`, reported to `on_denied` |
| **`keys.bind(...)`** | `KeyedCollection` | a `KeyedItem`; the ticket's `parent_id` does not change |

## At a glance

| Surface | Main names | Used by |
| --- | --- | --- |
| **Memory** | `Memory`, `MemoryHandle` | Every application |
| **Records** | `MemoryBlock`, `MemoryLink`, `LinkRelations` | Every application |
| **Query** | `MemoryQuery`, `Scope`, `SearchResult`, `matches_filters`, `normalize_filters` | Search callers, search adapters |
| **Policy** | `MemoryPolicy`, `AccessRule`, `Principal`, `PolicyEngine`, `Operation` | Applications that share blocks |
| **Structures** | `OwnershipTree`, `PropertyGraph`, `LineageDAG`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `VersionedRecords` | Applications that organize blocks |
| **Hooks** | `MemoryHook`, `HookRegistry`, `HookContext`, and their enums | Audit, metrics, validation |
| **Codecs** | `MemoryCodec`, `CodecRegistry`, `EncodedValue`, `JsonCodec`, stdlib codecs | Applications with non-JSON values |
| **Backend contracts** | `MemoryBackend` and the capability protocols | Adapter authors |
| **Adapters** | `InMemoryBackend`, `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, `RedisBackend`, `RoutedBackend` | Application setup code |
| **Errors** | `GenieError` family, `AccessDenied` | Every caller |

## Conventions

- **Every public name imports from `genie`** and from its subpackage
  (`genie.memory`, `genie.structures`, `genie.backend`, and so on). Both reach
  the same object.
- **Every call is synchronous.** No call returns a future, and hooks run on the
  calling thread.
- **Block arguments accept an id or a block.** Parameters typed
  `str | MemoryBlock` (`BlockRef` in the structures) read only the `id`.
  Authorization always runs against the stored block, never against the object
  passed in.
- **Options are keyword-only.** Only the main subject of a call (a block id, a
  value, a link, a query) is positional.
- **Returned objects are copies.** Records are frozen dataclasses with
  `slots=True`, and values are decoded and deep-copied, so editing a result
  never changes stored state.
- **A list call filters; a single-item call raises.** `children`, `ancestors`,
  `list_links`, `search`, and structure listings drop what the principal
  cannot read. `get`, `set`, and exact lookups raise `AccessDenied` instead.
- **A missing block is `None` only from `get`.** `Memory.get` and the structure
  `get` methods return `None` for an unknown key. Calls that need a block to
  exist raise `MemoryNotFound`.
- **Stale writes raise `MemoryConflict`.** It means "read again and retry". No
  call retries by itself.
- **Bad argument types raise `TypeError`; bad argument values raise
  `ValueError`.** A stored value that no registered codec can decode is also a
  `TypeError`.
- **Ids, block types, and relations are non-empty strings.** A block argument
  that is neither a `MemoryBlock` nor a string, a non-string `type` or `id` in
  `put`, and a non-string `relation` raise `TypeError`. An empty or
  whitespace-only string raises `ValueError`. A bad value never reaches the
  backend. The tables below do not repeat these errors.

## `Memory`

### Constructor

```python
Memory(*, backend=None, principal=None, policy_engine=None, hooks=None, codecs=None, default_policy=None, attributes=None)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `backend` | `MemoryBackend \| None` | `InMemoryBackend()` | Where blocks, links, and search live |
| `principal` | `Principal \| str \| None` | `Principal("system")` | Who acts; a string becomes `Principal(id=...)` |
| `policy_engine` | `PolicyEngine \| None` | `PolicyEngine()` | Evaluates `MemoryPolicy` rules |
| `hooks` | `list[MemoryHook] \| HookRegistry \| None` | empty `HookRegistry` | Fixed for the life of the object; its contents can change |
| `codecs` | `CodecRegistry \| None` | `CodecRegistry([JsonCodec()])` | Encodes and decodes values |
| `default_policy` | `MemoryPolicy \| None` | `MemoryPolicy.owner_private()` | Attached by `put` when no `policy` is given |
| `attributes` | `Mapping[str, str] \| None` | `{}` | Copied into every `HookContext` |

- **Raises:** `TypeError` for a wrong-typed argument; `ValueError` for an
  empty principal id or a repeated hook.
- **Holds no block data.** Two `Memory` objects over one backend see the same
  blocks.

### Blocks

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `put(*, type, value, parent=None, metadata=None, policy=None, content_type=None, id=None)` | `MemoryBlock` | `write` and `traverse_down` on the parent | `AccessDenied`, `MemoryNotFound` (parent), `TypeError`, `ValueError` |
| `get(block_id)` | `MemoryBlock \| None` | `read` | `AccessDenied`, `TypeError` (bad id or no codec), `ValueError` (empty id) |
| `set(block_id, value, *, content_type=None)` | `MemoryBlock` | `write` | `AccessDenied`, `MemoryConflict`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `update(block_id, *, value=None, metadata=None)` | `MemoryBlock` | `write` | `AccessDenied`, `MemoryConflict`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `delete(block_id)` | `None` | `write` on the target only | `AccessDenied`, `MemoryConflict`, `MemoryNotFound` |

- **`put` without `parent` creates a root block.** No parent check runs.
- **`id` is optional.** Without it, Genie generates a `mem_` id. An empty
  `id` is a `ValueError`; it does not fall back to a generated id.
- **`set` replaces the value** and may change `content_type`. **`update`
  merges** into a `dict` value and into metadata, and keeps `content_type`.
  `update(value=...)` on a non-`dict` value is a `TypeError`.
- **Both `set` and `update` raise `version` by `1`** and fail with
  `MemoryConflict` when another writer changed the block first.
- **`delete` removes the whole subtree** plus every link, key binding, log
  entry, and temporal entry that names a removed block.
- **`metadata` is always a strict JSON `dict`**, whatever codec the value uses.
- **`ValueError` from a write** means a `before_*` hook changed a field it may
  not change (for example the parent in `before_put`).

### Ownership

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `children(block_id)` | `list[MemoryBlock]` | `traverse_down` on the parent, then `read` per child | `AccessDenied`, `MemoryNotFound` |
| `ancestors(block_id)` | `list[MemoryBlock]`, nearest parent first | `traverse_up` on the block, then `read` per ancestor | `AccessDenied`, `MemoryNotFound` |
| `contains(root, block_id)` | `bool` | `traverse_down` on the root, `read` on the candidate | `AccessDenied`, `MemoryNotFound` (root) |

- **No call changes `parent_id`.** A block's parent is fixed at `put`.

### Links and mounts

| Method | Returns | Checked | Raises |
| --- | --- | --- | --- |
| `add_link(source, target, relation, *, metadata=None)` | `MemoryLink` | `write` on source, `read` on target | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `add_acyclic_link(source, target, relation, *, metadata=None)` | `MemoryLink` | same | also `MemoryConflict` (cycle), `UnsupportedOperation` (no `AcyclicLinkStore`) |
| `update_link(link, *, metadata)` | `MemoryLink` | same | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `list_links(block_id, *, relation=None, direction="out")` | `list[MemoryLink]` | `read` on the block and on both ends of each link | `AccessDenied`, `MemoryNotFound`, `ValueError` (direction) |
| `add_mount(source, target, *, metadata=None)` | `MemoryLink` with relation `"genie.mount"` | `write` on source, `read` on target | `AccessDenied`, `MemoryNotFound`, `TypeError`, `ValueError` |
| `list_mounts(source)` | `list[MemoryLink]` | `read` on source | `AccessDenied`, `MemoryNotFound` |
| `discover_mounts(root, *, include_self=True)` | `list[MemoryLink]` | `search` and `traverse_down` on root, `read` per source | `AccessDenied`, `MemoryNotFound` |
| `resolve_mount(mount)` | `MemoryBlock \| None` | `read` on source; target goes through `get` | `AccessDenied`, `MemoryNotFound` (source), `ValueError` (not a mount) |

- **`direction`** is `"out"`, `"in"`, or `"both"` (`LinkDirection`).
- **`update_link` merges metadata only.** Source, target, and relation never
  change.
- **`add_acyclic_link` refuses `"genie.mount"`** with `ValueError`.
- **Links do not imply ownership.** Recursive delete and subtree search do not
  follow them.

### Search

```python
search(query=None, *, text=None, root=None, scope=Scope.GLOBAL, types=(), filters=None, limit=20)
```

- **Three input forms:** a `MemoryQuery`, a text string as `query`, or keyword
  arguments. All become one `MemoryQuery`.
- **Returns** `list[SearchResult]` that the principal can `search` and `read`
  and that fall inside the scope.
- **`limit` is a candidate budget.** The backend spends it before policy
  filtering, so the visible page can be shorter than `limit`.
- **Raises:** `AccessDenied` (root not searchable or not traversable),
  `MemoryNotFound` (root), `TypeError`, `ValueError` (unknown scope, negative
  limit, or a non-global scope with no root).

### Handles

| Method | Returns | Raises |
| --- | --- | --- |
| `get_handle(*, root, principal=None, default_scope=Scope.SUBTREE, allow_up=False, allow_global=False, allow_mounts=False, allow_policy_override=False)` | `MemoryHandle` bounded to `root` | `AccessDenied` (handle principal cannot `read` root), `MemoryNotFound`, `TypeError`, `ValueError` |
| `get_unbounded_handle(*, principal=None, default_scope=Scope.SUBTREE, ...same flags)` | `MemoryHandle` with no root | `TypeError`, `ValueError` |

- **`principal=None` keeps this memory's principal.**
- **`default_scope` is checked against the flags.** For example
  `default_scope="global"` without `allow_global=True` is a `ValueError`.
- **`hooks`** is a read-only property that returns the `HookRegistry`.

## `MemoryHandle`

A bounded view of one `Memory`. It has the same block, ownership, link, mount,
and search methods, with these differences:

| Difference | Handle behavior |
| --- | --- |
| **Boundary** | Every id has to be inside the root. An unknown id is refused as "outside the handle" before any lookup, so its existence is not revealed |
| **Optional root argument** | `children()`, `ancestors()`, and `discover_mounts()` default to the handle root. On an unbounded handle, leaving it out is `AccessDenied` |
| **Search** | `scope=None` uses `default_scope`; `root` has to be inside the handle; the query is rebuilt so a prebuilt `MemoryQuery` cannot widen it; a query rewritten by `before_search` is checked against the handle again |
| **Explicit policy** | `put(policy=...)` needs `allow_policy_override` |
| **No `contains`** | Not part of the handle surface |
| **`attributes`** | Read-only property with the hook attributes this handle reports |

| Flag | `HandleCapability` | What it allows |
| --- | --- | --- |
| rooted handle | `SUBTREE` | Nothing outside the root |
| `allow_up` | `ANCESTORS` | `ancestors()` and `scope="ancestors"` |
| `allow_global` | `GLOBAL_SEARCH` | `scope="global"` |
| `allow_mounts` | `MOUNTS` | `add_mount`, `list_mounts`, `discover_mounts`, `resolve_mount` |
| `allow_policy_override` | `POLICY_OVERRIDE` | `put(policy=...)` |
| unbounded handle | `ROOT_REQUIRED` | Refuses calls that need a root it cannot infer |
| child handle | `PRINCIPAL` | Refuses a different principal |

- **`get_handle` on a handle only narrows.** The child root has to be inside
  the parent root, every flag has to be on in the parent, and a different
  principal raises `AccessDenied`.
- **A handle refusal is `AccessDenied`** and is reported to `on_denied` with
  the `HandleCapability` that refused.
- **Direct construction** (`MemoryHandle(memory=..., root_id=..., principal=...)`)
  is possible, but `get_handle` is the checked path.

## Records

| Type | Constructor | Notes |
| --- | --- | --- |
| `MemoryBlock` | `(type, value, id=<mem_...>, parent_id=None, content_type="application/json", metadata={}, policy=None, created_at, updated_at, created_by=None, version=1)` | `with_updates(**changes)` returns a new block |
| `MemoryLink` | `(source_id, target_id, relation, id=<link_...>, metadata={}, created_at)` | Only `metadata` can change |
| `LinkRelations` | `LinkRelations.MOUNT == "genie.mount"` | The only reserved relation |

- **Applications read these; they rarely build them.** `Memory` fills
  `created_by`, the times, and `version`. Adapter authors build them directly.
- **Field rules** are in [Data model](10-data-model.md#records).

## Query

### `MemoryQuery`

```python
MemoryQuery(text=None, root_id=None, scope=Scope.GLOBAL, types=(), filters=None, limit=20)
```

- **Frozen after `__init__`.** The scope becomes a `Scope`, `types` becomes a
  tuple, and filters are normalized.
- **`limit`:** `0` is allowed; a negative value is `ValueError`.
- **`text` meaning is the backend's.** `InMemoryBackend` matches a
  case-insensitive substring; a search adapter may rank with its own method.

### `Scope`

| Value | Results come from |
| --- | --- |
| `Scope.SELF` (`"self"`) | the root block only |
| `Scope.SUBTREE` (`"subtree"`) | the root and its descendants |
| `Scope.ANCESTORS` (`"ancestors"`) | the root and its ancestors |
| `Scope.LINKS` (`"links"`) | the root and blocks linked to it in either direction |
| `Scope.GLOBAL` (`"global"`) | every block; no root needed |

### Filters

- **Shape:** `{field: value}` for equality, or `{field: {operator: value}}`.
- **Field lookup:** a `metadata.` prefix reads metadata; a `MemoryBlock` field
  name reads that field; any other name reads metadata.
- **Operators:** `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`. Aliases
  `==`, `=`, `!=`, `>`, `>=`, `<`, `<=`, and `in` are rewritten to them.
- **A comparison of mismatched types is `False`,** not an error. An unknown
  operator never matches.
- **`normalize_filters(filters)`** returns the canonical form.
  **`matches_filters(block, filters)`** is the portable matcher adapters reuse.

### `SearchResult`

- **Fields:** `block` (`MemoryBlock`), `score` (`float`, default `1.0`),
  `reason` (`str | None`).
- **Score meaning is the backend's.** Genie does not compare scores across
  backends.

## Policy

| Name | Signature | Meaning |
| --- | --- | --- |
| `MemoryPolicy` | `(read, write, search, traverse_up, traverse_down, operations={})` | One `AccessRule` per built-in operation, plus named extra rules; each built-in field defaults to `AccessRule.owner()` |
| `MemoryPolicy.owner_private()` | classmethod | Only `created_by` may do anything; the default |
| `MemoryPolicy.public()` | classmethod | Everyone may do everything |
| `MemoryPolicy.owner_or(*, principals=None, roles=None)` | classmethod | The owner plus the listed principals and roles |
| `AccessRule` | `(allow_all=False, principals=frozenset(), roles=frozenset(), allow_owner=False)` | One rule; `AccessRule()` denies everyone |
| `AccessRule.allow()`, `.deny()`, `.owner()` | classmethods | Everyone, no one, the owner only |
| `AccessRule.only(*, principals=None, roles=None, owner=False)` | classmethod | An explicit list |
| `AccessRule.allows(principal, *, owner_id=None)` | method | `bool` |
| `Principal` | `(id="system", roles=frozenset(), metadata={})` | Who acts |
| `Principal.coerce(value)` | classmethod | `Principal`, `str`, or `None` to `Principal` |
| `PolicyEngine` | `(*, operations=None)` | Default rules for named extra operations |
| `PolicyEngine.allows(operation, block, principal)` | method | `bool` |
| `PolicyEngine.require(operation, block, principal)` | method | `None`, or `AccessDenied` |

- **`Operation`** values: `READ`, `WRITE`, `SEARCH`, `TRAVERSE_UP`,
  `TRAVERSE_DOWN`. An `operation` argument also accepts a string name.
- **Defaults fail closed.** `AccessRule()` denies everyone, and
  `MemoryPolicy()` equals `MemoryPolicy.owner_private()`. A policy that sets
  only some fields keeps the rest owner-only.
- **`allow_all=True` with `principals`, `roles`, or `allow_owner` is a
  `ValueError`,** because `allow_all` would make those grants have no effect.
- **An unknown operation name is a `ValueError`,** never a silent allow.
- **Extra rules cannot reuse a built-in name.** That is a `ValueError` at
  construction.
- **A block's policy is fixed at `put`.** No call changes it.

## Structure facades

- **Every facade takes an access object first:** a `Memory` or a
  `MemoryHandle`. Block reads and writes go through it, so handle and policy
  checks still apply.
- **Facades that keep records also take a store.** It is a backend with the
  named capability, or a `BlockRouter`, which resolves to the route that owns
  the container block.
- **Construction checks the capability** and raises `UnsupportedOperation`
  when the store lacks it. It also resolves the container block through the
  access object, so an invisible container raises `MemoryNotFound`.
- **`hooks=None`** uses the access object's registry.
- **Paged `limit` counts visible items,** unlike search, and defaults to `100`.

| Facade | Constructor | Store capability |
| --- | --- | --- |
| `OwnershipTree` | `(access, *, root)` | none |
| `PropertyGraph` | `(access)` | none |
| `LineageDAG` | `(access, *, relation)` | `AcyclicLinkStore`, through the access object |
| `KeyedCollection` | `(access, store, *, collection, hooks=None)` | `KeyedStore` |
| `EventLog` | `(access, store, *, log, hooks=None)` | `SequenceStore` |
| `TemporalCollection` | `(access, store, *, collection, index=None, hooks=None)` | `TemporalStore`, optional `TemporalIndex` |
| `VersionedRecords` | `(access, store, *, collection, hooks=None)` | `VersionedStore` (`KeyedStore` + `SequenceStore`) |

### `OwnershipTree`

| Method | Returns |
| --- | --- |
| `root` (property) | `MemoryBlock` |
| `create_child(*, type, value, parent=None, metadata=None, policy=None, content_type=None, id=None)` | `MemoryBlock`; `parent=None` means the tree root |
| `children(block=None)` | `list[MemoryBlock]` |
| `parent(block)` | `MemoryBlock \| None`; `None` at the tree root |
| `ancestors(block, *, include_self=False)` | `list[MemoryBlock]`, never above the tree root |
| `descendants(block=None, *, include_self=False)` | `list[MemoryBlock]`, depth-first pre-order |
| `contains(block)` | `bool` |
| `delete_subtree(block)` | `None` |

- **A block outside the tree is `AccessDenied`.**

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
| `add(predecessor, successor, *, metadata=None)` | `MemoryLink`; `MemoryConflict` on a cycle |
| `update_edge(edge, *, metadata)` | `MemoryLink`; `MemoryConflict` for another relation's edge |
| `edges(block, *, direction="out")` | `list[MemoryLink]` |
| `predecessors(block)`, `successors(block)` | `list[GraphNeighbor]` |
| `walk_predecessors(block, *, max_depth=6, limit=100)`, `walk_successors(...)` | `list[GraphPath]` |
| `shortest_path(predecessor, successor, *, max_depth=6)` | `GraphPath \| None` |

- **`LineageDAG` works on one relation.** An empty relation or
  `"genie.mount"` is a `ValueError`.
- **Relation names are shared across the whole backend.** Protection starts
  with the first acyclic insert and then covers every block with that
  relation, across handles and tenants. Applications prefix lineage relations,
  such as `"billing.derived_from"`, to avoid accidental clashes.
- **`GraphNeighbor`:** `block`, `link`, `direction` (`"out"` or `"in"`).
  **`GraphPath`:** `block_ids`, `links`.

### `KeyedCollection`

| Method | Returns | Raises |
| --- | --- | --- |
| `collection` (property) | `MemoryBlock` | |
| `bind(address, block)` | `KeyedItem` | `MemoryConflict` (address in use), `MemoryNotFound` |
| `rebind(address, block, *, expected)` | `KeyedItem` | `MemoryConflict` (missing, or not `expected`) |
| `unbind(address, *, expected)` | `None` | `MemoryConflict` |
| `get(address)` | `MemoryBlock \| None` | `MemoryNotFound` (dangling binding) |
| `get_item(address)` | `KeyedItem \| None` | `MemoryNotFound` |
| `items(*, namespace=None, recursive=False, limit=100)` | `list[KeyedItem]`, ascending address | `TypeError`, `ValueError` |

- **`MemoryAddress(namespace, key)`:** `namespace` is a tuple of non-empty
  strings (`()` is the root), `key` is a non-empty string.
- **`rebind` and `unbind` are compare-and-swap** on `expected`.

### `EventLog`

| Method | Returns |
| --- | --- |
| `log` (property) | `MemoryBlock` |
| `append(block)` | `LogItem` with the allocated `sequence` |
| `get(sequence)` | `LogItem \| None` |
| `read(*, start=None, stop=None, reverse=False, limit=100)` | `list[LogItem]`, half-open `[start, stop)` |
| `tail(*, limit=20)` | `list[LogItem]`, newest items in oldest-first order |

- **Sequences start at `0` and are never reused.** A deleted block leaves a gap.

### `TemporalCollection`

| Method | Returns | Raises |
| --- | --- | --- |
| `collection` (property) | `MemoryBlock` | |
| `add(block, *, occurred_at=None, validity=None)` | `TemporalItem` | `MemoryConflict` (already a member) |
| `replace(block, *, occurred_at=None, validity=None)` | `TemporalItem` | `MemoryConflict` (not a member) |
| `remove(block)` | `None`; the block stays | `MemoryConflict` (not a member) |
| `get(block)` | `TemporalItem \| None` | `MemoryNotFound` (dangling entry) |
| `events(*, start=None, stop=None, limit=100)` | `list[TemporalItem]` with `occurred_at` in `[start, stop)` | `TypeError`, `ValueError` (naive time) |
| `valid_at(when, *, limit=100)` | `list[TemporalItem]` | `TypeError`, `ValueError` |
| `overlapping(interval, *, limit=100)` | `list[TemporalItem]` | `TypeError`, `ValueError` |
| `scan(*, occurred_start=None, occurred_stop=None, valid_at=None, overlaps=None, limit=100)` | `list[TemporalItem]` matching every given filter | |

- **`ValidityInterval(valid_at, invalid_at=None)`** is half-open;
  `invalid_at=None` means open-ended.
- **All times are timezone-aware** and normalized to UTC. A naive `datetime` is
  a `ValueError`.
- **`index`** is used when given; without it the facade scans and matches.

### `VersionedRecords`

| Method | Returns |
| --- | --- |
| `collection` (property) | `MemoryBlock` |
| `register(address, identity)` | `KeyedItem`; `MemoryConflict` if already registered |
| `identity(address)` | `MemoryBlock \| None` |
| `identities(*, namespace=None, recursive=False, limit=100)` | `list[KeyedItem]` |
| `append(address, block)` | `RevisionItem`; `MemoryNotFound` if no identity |
| `current(address)` | `RevisionItem \| None` |
| `revision(address, revision)` | `RevisionItem \| None` |
| `history(address, *, start=1, stop=None, reverse=False, limit=100)` | `list[RevisionItem]`, half-open |

- **Revisions start at `1`.** `revision < 1` is a `ValueError`.
- **Each revision is its own block.** `append` does not change the block it
  points at.

### Read results

| Type | Fields |
| --- | --- |
| `KeyedItem` | `binding` (`KeyBinding`), `block` |
| `LogItem` | `entry` (`LogEntry`), `block` |
| `TemporalItem` | `entry` (`TemporalEntry`), `block` |
| `RevisionItem` | `entry` (`RevisionEntry`: `record_id`, `revision`, `block_id`), `block` |

### Access protocols

- **Facades declare the narrow access they need,** as `runtime_checkable`
  protocols. A custom access object only has to implement those methods.

| Protocol | Methods |
| --- | --- |
| `BlockReader` | `get` |
| `BlockWriter` | `put`, `set`, `update`, `delete` |
| `TreeReader` | `children`, `ancestors` |
| `LinkReader` | `list_links` |
| `LinkWriter` | `add_link`, `update_link` |
| `AcyclicLinkWriter` | `add_acyclic_link` |
| `SearchAccess` | `search` |
| `OwnershipAccess` | `BlockReader` + `TreeReader` + `put`, `delete` |
| `GraphAccess` | `BlockReader` + `LinkReader` + `LinkWriter` |
| `LineageAccess` | `GraphAccess` + `AcyclicLinkWriter` |
| `KeyedAccess`, `SequenceAccess`, `TemporalAccess`, `VersionedAccess` | `BlockReader` |
| `MemoryAccess` | all of the above; `Memory` and `MemoryHandle` satisfy it |

## Hooks

### `MemoryHook`

- **A base class with no-op callbacks.** A hook overrides only what it needs.
- **`fail_open: bool = False`.** When `True`, a failure in an observing
  callback is logged and dropped.

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
| `before_delete(context, block)` | mutating | `None`; once per block in the subtree |
| `after_put(context, block)` | observing | `None` |
| `after_update(context, previous, stored)` | observing | `None` |
| `after_get(context, block)` | observing | `None`; `block` may be `None` |
| `after_traverse(context, traversal)` | observing | `None` |
| `after_add_link(context, link)` | observing | `None` |
| `after_update_link(context, previous, stored)` | observing | `None` |
| `after_structure_change(context, change)` | observing | `None` |
| `after_delete(context, block)` | observing | `None`; once per removed block |
| `on_denied(context, operation, subject_id, reason)` | observing | `None` |
| `on_suppressed(context, suppression)` | observing | `None` |
| `on_operation_end(context, outcome, error)` | observing | `None`; exactly once per started operation |

- **Mutating callbacks stop at the first failure** and the error leaves the
  operation. `fail_open` does not apply to them.
- **Observing callbacks all run,** then the first failure is raised (or
  attached as a note when the operation already failed).
- **What a `before_*` hook returns is validated.** A change to a protected
  field is a `ValueError`; a wrong return type is a `TypeError`.
- **`after_delete` fans out from one call to many.** `Memory.delete` calls
  `HookRegistry.after_delete(context, *blocks)` once with every removed block.
  The registry then calls each hook's `after_delete(context, block)` once per
  block, and raises failures only after every block has reached every hook.

### `HookRegistry`

| Member | Returns | Raises |
| --- | --- | --- |
| `HookRegistry(hooks=None, *, max_depth=16)` | registry | `TypeError`, `ValueError` (repeat hook, negative depth) |
| `hooks`, `mandatory` (properties) | `tuple` of hooks, in dispatch order | |
| `register(hook, *, mandatory=False)` | `None` | `TypeError`, `ValueError` (already registered) |
| `unregister(hook)` | `None` | `ValueError` (not registered, or mandatory) |
| `scope(name, *, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError` |
| `suppress(*hooks, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError`, or the first `on_suppressed` failure |
| `operation(operation, *, principal_id=None, attributes=None)` | context manager of `HookContext` | `HookRecursionError`, `TypeError`, `ValueError` |

- **`scope` groups operations** under one parent context and dispatches
  nothing itself.
- **`suppress()` with no hooks silences the whole registry;** with hooks it
  silences only those. Mandatory hooks are never silenced.
- **`operation` is for code that extends Genie,** such as a custom facade. The
  dispatch methods (`before_put`, `after_put`, and so on) are for the same use.

### Context and event types

| Type | Fields or values |
| --- | --- |
| `HookContext` | `operation_id`, `parent_operation_id`, `operation`, `origin`, `depth`, `principal_id`, `attributes`, `started_at`, `scope_name`; `is_scope` property |
| `HookOperation` | `put`, `get`, `set`, `update`, `delete`, `search`, `children`, `ancestors`, `contains`, `link.add`, `link.add_acyclic`, `link.update`, `link.list`, `mount.add`, `mount.list`, `mount.discover`, `mount.resolve`, `handle.create`, `key.bind`, `key.rebind`, `key.unbind`, `log.append`, `temporal.add`, `temporal.replace`, `temporal.remove`, `hooks.suppress`, `scope` |
| `HookOrigin` | `"caller"`, `"hook"` |
| `HookOutcome` | `SUCCESS`, `DENIED`, `ERROR` |
| `Traversal` | `kind`, `origin_id`, `target_id`, `relation`, `direction`, `blocks`, `links`, `contained` |
| `TraversalKind` | `CHILDREN`, `ANCESTORS`, `LINKS`, `CONTAINS`, `MOUNTS`, `SUBTREE_MOUNTS` |
| `StructureChange` | `kind`, `action`, `container_id`, `block_id`, `before`, `after` |
| `StructureKind` | `KEYED_COLLECTION`, `EVENT_LOG`, `TEMPORAL_COLLECTION` |
| `StructureAction` | `BIND`, `REBIND`, `UNBIND`, `APPEND`, `ADD`, `REPLACE`, `REMOVE` |
| `Suppression` | `scope` (`SuppressionScope.SELECTED` or `REGISTRY`), `hooks`, `mandatory` |
| `HandleCapability` | see [`MemoryHandle`](#memoryhandle) |

- **`freeze_attributes(attributes)`** validates a `str` to `str` mapping and
  returns a read-only copy.

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

- **Only `JsonCodec` is registered by default.** The others are opt-in.
- **Encode picks a codec** by explicit `content_type`, or else the first
  registered codec whose `can_encode` is `True`. **Decode picks** by the stored
  `content_type` only.
- **An unregistered content type is a `TypeError`,** on both encode and decode.
- **A registry passed to `Memory` replaces the default.** Include `JsonCodec()`
  in it to keep JSON values working.

## Backend contracts

Adapter authors implement these. Application code rarely calls them, and a
direct call skips handles, policies, hooks, and codecs.

| Protocol | Methods |
| --- | --- |
| `BlockStore` | `put_block(block)`, `get_block(block_id)`, `update_block(block, *, expected_version=None)`, `delete_block(block_id, *, expected_versions=None)` |
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

- **Every protocol is `runtime_checkable`.** Genie checks capabilities with
  `isinstance`, so an adapter claims a capability by implementing its methods.
- **Only `MemoryBackend` is required** by `Memory`. The rest are optional, and
  a facade that needs one raises `UnsupportedOperation` without it.
- **Conditional and uniqueness checks happen inside one store call:**
  `expected_version`, `expected_versions`, `expected_block_id`, address
  uniqueness, cycle checks, sequence allocation, and temporal membership. A
  failed check is `MemoryConflict`.
- **`delete_block` cascades** to links, key bindings, log entries, and temporal
  entries in the same atomic step.
- **The shared contract tests** in `lib/tests/backends/test_backend_contracts.py`
  define the expected behavior. See [Testing strategy](11-testing-strategy.md).

## Adapters

| Class | Constructor | Extra |
| --- | --- | --- |
| `InMemoryBackend` | `()` | none |
| `SQLiteBackend` | `(path=":memory:", *, timeout=5.0, durability="full")` | none |
| `PostgresBackend` | `(conninfo, *, schema="genie", timeout=5.0, connect_timeout=30.0, durability="full", verify_server_role=True, initialize_schema=True)` | `postgres` |
| `Neo4jBackend` | `(uri, *, auth=None, database="neo4j", connection_timeout=30.0, max_transaction_retry_time=30.0, encrypted=None, verify_connectivity=True, initialize_schema=True)` | `neo4j` |
| `MongoDBBackend` | `(uri, *, auth=None, database="genie", connect_timeout=30.0, socket_timeout=30.0, max_retry_time=30.0, durability="full", verify_server_role=True, verify_transaction_support=True, initialize_schema=True)` | `mongodb` |
| `RedisBackend` | `(url, *, auth=None, key_prefix="genie", connect_timeout=10.0, socket_timeout=30.0, max_retry_time=30.0, health_check_interval=30.0, transport_retries=3, verify_connectivity=True, verify_server_role=True, verify_eviction_policy=True, initialize_schema=True)` | `redis` |
| `RoutedBackend` | `(routes, *, route_selector=None, default_route=None, traversal_index=None, search_index=None, link_store=None)` | none |
| `InMemoryLinkStore` | `()` | none |

- **Every storage adapter implements every optional capability:**
  `AcyclicLinkStore`, `KeyedStore`, `SequenceStore`, `TemporalStore`, and
  `TemporalIndex`. `RoutedBackend` implements `BlockRouter` and
  `AcyclicLinkStore`, and sends each acyclic link to the store that holds its
  links; `InMemoryLinkStore` is a standalone `LinkStore`.
- **A missing driver raises `UnsupportedOperation`** at construction, naming
  the extra to install (for example `genie[postgres]`).
- **`verify_*` flags** turn off a construction-time server check.
  **`initialize_schema=False`** uses an existing layout without creating it.
- **`RoutedBackend`:** `routes` maps a `RouteKey` (`str`) to a backend;
  `route_selector` is a `RouteSelector`, a callable from `MemoryBlock` to
  `RouteKey | None`. Extra members: `routes`, `register_route(route, backend,
  *, replace=False, make_default=False)`, `backend_for_block(block_id)`,
  `route_for_block(block_id)`. `RouteInfo(block_id, route)` is its catalog
  record.
- **Adapter options, schema, and atomicity notes** are in each adapter's
  README under `lib/src/genie/backend/`.

## Errors

| Error | Base | Raised when |
| --- | --- | --- |
| `GenieError` | `Exception` | Base of every Genie error except `AccessDenied` |
| `AccessDenied` | `PermissionError` | A policy or handle boundary refuses the call |
| `MemoryNotFound` | `GenieError` | A block, link, or root the call needs does not exist, or a structure record points at a missing block |
| `MemoryConflict` | `GenieError` | A version, snapshot, key, cycle, or membership check fails |
| `UnsupportedOperation` | `GenieError` | A backend lacks a capability, or an adapter driver is missing |
| `HookRecursionError` | `GenieError` | Hook-started operations nest deeper than `max_depth` |
| `BackendError` | `GenieError` | Base of the adapter failures below |
| `BackendBusy` | `BackendError` | The store is locked or overloaded; a retry may succeed |
| `BackendUnavailable` | `BackendError` | The store cannot be reached |
| `BackendReadOnly` | `BackendError` | The store refuses writes, for example a replica |
| `BackendCapacityExceeded` | `BackendError` | The store is out of space or memory |
| `BackendCorruption` | `BackendError` | Stored data or layout cannot be read safely |

- **`AccessDenied` is a `PermissionError`,** so `except GenieError` does not
  catch it. Catch it by name.
- **Adapters keep the driver error as `__cause__`** on every `BackendError`.

## Left out on purpose

- **No async API.** Every call is synchronous.
- **No batch or transaction API.** Each public call is one backend write.
- **No reparenting and no policy change.** `parent_id` and `policy` are fixed
  at `put`.
- **No server, CLI, or network protocol.** Genie is a library imported by the
  application.
- **No block types, relation names, or value meanings** beyond
  `"genie.mount"`.

## Related reading

- [High-level architecture](03-high-level-architecture.md) — components and extension seams
- [Detailed architecture](04-detailed-architecture.md) — check order, hook dispatch, and search pipeline
- [Data model](10-data-model.md) — record fields and integrity rules
- [Testing strategy](11-testing-strategy.md) — contract suites
- [Memory](../lib/src/genie/memory/README.md), [Structures](../lib/src/genie/structures/README.md), [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md)
