# Queries

`genie.query` defines portable search requests and results. It does not choose a
ranking method or a search engine for you.

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

- **`Memory.search` normalizes** these friendly arguments into a `MemoryQuery`: optional `text`, an optional `root_id`, one `Scope`, optional `types` and `filters`, and a `limit` that cannot be negative.
- **Results are ranked `SearchResult` values.** Each holds a `block`, a finite numeric `score`, and a `reason`.
- **Building a query checks runtime types** and copies the type lists and nested filter operands that the caller owns.

### `limit` is a backend budget, not a result count

- **The backend applies `limit` first.** Then `Memory` removes results the principal cannot `read`, and results outside the query `Scope`.
- **So a search can return fewer results than the `limit`,** even when more matching blocks exist. This is expected behavior.
- **There is no result cursor.** A caller who needs a full page asks for a larger `limit` and cuts the list itself, and decides how much extra backend work that is worth.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | Blocks linked to the root |
| `global` | All blocks the backend can see |

- **A `root` is required** for every `Scope` except `global` when you search through `Memory`.
- **Scopes resolve from stored state, not from the query.** `subtree` and `ancestors` come from stored parents, and `links` comes from stored links. A search index that holds neither one needs traversal and link access to resolve them.
- **No scope resolves structure membership.** Key bindings, log entries, temporal entries, and revisions record membership by block id and do not change stored parents. So a search rooted at a container block finds only the members that the application also created as children of that container.
- **A bounded handle cannot escape through search.** It also cannot use search to turn on the `allow_global` or `allow_up` access that the handle has disabled.

## Filters

- **Address a block field or a metadata field** such as `metadata.priority`.
- **A name with no prefix** matches a declared `MemoryBlock` field first, then metadata.
- **`$in` needs a list-like collection.**
- **An invalid or empty operator map fails closed,** which means it is rejected rather than ignored, so a broken filter can never widen the results.

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

Every shipped adapter uses the same portable matcher, and the persistent ones
scan candidates inside the requested scope. Another adapter may use richer
indexes, as long as the public meaning of a query stays the same.

## Authorization flow

1. Genie checks the requested root and scope.
2. `before_search` may change the normalized query.
3. Genie authorizes the changed query again. For a handle search, it also checks the changed query against the handle's root, `allow_global`, and `allow_up`.
4. The backend searches.
5. Genie decodes the results and filters them by policy.
6. `after_search` may filter or reorder them.
7. Genie applies the scope and policy filters one more time.

Steps 3 and 7 are what stop a hook or a backend result from widening access.

Related: [Memory](../memory/README.md), [Backends](../backend/README.md),
[Policies](../policy/README.md), and [Hooks](../hooks/README.md).
