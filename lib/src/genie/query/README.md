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

- **`Memory.search` normalizes** these friendly arguments into a `MemoryQuery`. The query has optional `text`, an optional `root_id`, one `Scope`, optional `types` and `filters`, and a `limit` that cannot be negative.
- **Results are ranked `SearchResult` values.** Each one holds a `block`, a finite numeric `score`, and an optional `reason`.
- **A new query checks runtime types.** It also copies the type lists and the nested filter operands that the caller owns.

### `limit` is a backend budget, not a result count

- **The backend applies `limit` first.** Then `Memory` removes the results that the principal cannot `search` or `read`, and the results outside the query `Scope`.
- **So a search can return fewer results than the `limit`,** also when more blocks match. This is expected behavior.
- **There is no result cursor.** A caller that needs a full page asks for a larger `limit` and cuts the list itself. The caller also decides how much extra backend work that is worth.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | The root and the blocks linked to it, in either direction. A mount target is in scope, but its subtree is not |
| `global` | All blocks that the backend can see |

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

- **Same matcher:** each shipped adapter uses the same portable matcher, `matches_filters`. The persistent adapters scan the candidates inside the requested scope.
- **Other adapters:** an adapter can use richer indexes, if the public meaning of a query stays the same.

## Authorization flow

1. Genie checks the requested root and scope: `search` on the root, and also `traverse_down` for `subtree`, `traverse_up` for `ancestors`, or `read` for `links`.
2. `before_search` can change the normalized query.
3. Genie authorizes the changed query again. For a handle search, it also checks the changed query against the root, `allow_global`, and `allow_up` of the handle.
4. The backend searches.
5. Genie decodes the results, and it filters them by policy and by scope.
6. `after_search` can filter or reorder them.
7. Genie applies the scope and policy filters one more time.

Steps 3, 5, and 7 make sure that a hook or a backend result cannot widen access.

Related: [Memory](../memory/README.md), [Backends](../backend/README.md),
[Policies](../policy/README.md), and [Hooks](../hooks/README.md).
