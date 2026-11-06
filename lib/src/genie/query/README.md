# Queries

`genie.query` defines portable search requests and results. It does not choose
a ranking method or a search engine.

## Requests and scopes

```python
results = memory.search(
    "concise",
    root=project,
    scope="subtree",
    types=["requirement"],
    filters={"metadata.priority": {">=": 3}},
    limit=20,
)
```

- **`Memory.search` normalizes** these friendly arguments into a `MemoryQuery`. Next to a `MemoryQuery`, each keyword that is not `None` replaces the same field of the query. The query has optional `text`, an optional `root_id`, an optional `Scope`, optional `types` and `filters`, a `limit` that cannot be negative, and an optional `after` cursor.
- **Results are ranked `SearchResult` values.** Each one holds a `block`, a finite numeric `score`, an optional `reason`, and an optional keyword-only `position`. Its `cursor` property gives the position after it: `position` if it is set, or else a position from `score`, `block.created_at`, and `block.id`. Equality ignores `position`.
- **A new query checks runtime types.** It also copies the type lists and the nested filter operands that the caller owns.
- **A new query refuses empty values.** An empty `root_id` or `types` entry, or one of only whitespace, raises `ValueError`, as an empty id or block type does in the rest of the API.

### Search order, `limit`, and `after`

- **Search order:** results sort by `score` from high to low, then by `created_at` from old to new, then by block id. So results with the same score come in creation order.
- **`limit` counts visible results.** `Memory` removes the results that the principal cannot `search` or `read`, and the results outside the query `Scope`, before it counts. So blocks that the caller cannot see do not change which visible results it gets.
- **`Memory` gets backend pages.** The first page has at most `limit` results. Each next page starts after the last result and is 2 times larger. `Memory` stops when it has `limit` visible results, or when the backend has no more results. There is no cap, because a cap would let hidden blocks change the results again.
- **The cost:** if the caller can see only a few of many matching blocks, one search calls the backend many times.
- **Next page:** set `after` to the `cursor` of the last result, and keep the other arguments. `MemoryQuery` also has an `after` field. With a `MemoryQuery`, `search(query, after=cursor)` also works, because a search keyword replaces the same field of the query.

```python
first = memory.search("concise", limit=20)
second = memory.search("concise", limit=20, after=first[-1].cursor)
```

- **A handle pages the same way.** A search with no scope, as a keyword or in a `MemoryQuery`, uses the `default_scope` of the handle.
- **The cursor follows the backend order.** `Memory.search` puts the stored block in each result, and it sets `position` to the place of the backend result. A separate search index can keep a different `created_at`, for example at millisecond precision, or an old copy of a block id. The cursor then still names a place that the index uses, so no result is lost.
- **A cursor is only a position.** A `SearchCursor` holds a `score`, a `created_at`, and a block id. It can name a block that the caller cannot see, and it shows nothing about that block. With a separate search index, its `score` and `created_at` come from the index copy.
- **`page_results` sorts by `cursor`.** So an application can merge the pages of 2 `Memory` searches with it, and page the merged list with the same cursor.
- **Pages are not a snapshot.** A block that changes between 2 calls can move in the order. One search counts each block one time. But a block can be missing from a later page, or it can be on 2 pages.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | The root and the blocks linked to it, in either direction. A mount target is in scope, but its subtree is not |
| `global` | All blocks that the backend can see |

- **`scope=None` (the default of `MemoryQuery`) means the default scope of the caller.** `Memory` uses `global`, and a `MemoryHandle` uses its `default_scope`. `Memory` gives hooks and backends a `Scope`. A `before_search` hook that returns `scope=None` gets `global`.
- **Each `Scope` except `global` needs a `root`** when you search through `Memory`. Without one, `Memory.search` raises `ValueError`.
- **Scopes resolve from stored state, not from the query.** `subtree` and `ancestors` come from stored parents, and `links` comes from stored links. A search index that holds neither one needs traversal and link access to resolve them.
- **No scope resolves structure membership.** Key bindings, log entries, temporal entries, and revisions record membership by block id, and they do not change stored parents. So a search with a container block as its root finds only the members that the application also created as children of that container.
- **A bounded handle cannot escape through search.** Search also cannot turn on the `allow_global` or `allow_up` access that the handle has off.

## Filters

- **A filter addresses a block field or a metadata field,** such as `metadata.priority`.
- **A name with no prefix** matches a declared `MemoryBlock` field first, then metadata.
- **`$in` needs a list-like collection:** a `list`, `tuple`, `set`, or `frozenset`. Any other operand matches nothing.
- **A comparison that cannot run matches nothing.** `$gt`, `$gte`, `$lt`, and `$lte` do not match `None`, for example from a metadata key that does not exist. They also do not match a value of a type that does not compare.
- **An unknown operator raises `ValueError`** when the query is built. So a typo such as `>>=` fails before any backend runs, and it does not return an empty result.
- **An empty operator map fails closed:** it matches nothing. So a broken filter can never widen the results.

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

- **Same matcher:** each shipped adapter uses the same portable matcher, `matches_filters`, and the same order helper, `page_results`. The persistent adapters scan the candidates inside the requested scope on each call.
- **Other adapters:** an adapter can use richer indexes, if the public meaning of a query stays the same. It returns results in the search order, after `after`, and it gives a block the same score on each call.
- **A score uses only the block and the query.** A score that uses data from other blocks, such as corpus statistics, lets blocks that the caller cannot see change the order of the visible results.

## Authorization flow

1. Genie checks the requested root and scope: `search` on the root, and also `traverse_down` for `subtree`, `traverse_up` for `ancestors`, or `read` for `links`.
2. `before_search` can change the normalized query.
3. Genie authorizes the changed query again. For a handle search, it also checks the changed query against the root, `allow_global`, and `allow_up` of the handle.
4. The backend returns one page.
5. Genie filters the page by policy and by scope, and it decodes only the results that it keeps. It gets the next page until it has `limit` visible results.
6. `after_search` can filter or reorder them.
7. Genie applies the scope and policy filters one more time.

Steps 3, 5, and 7 make sure that a hook or a backend result cannot widen access.

Related: [Memory](../memory/README.md), [Backends](../backend/README.md),
[Policies](../policy/README.md), and [Hooks](../hooks/README.md).
