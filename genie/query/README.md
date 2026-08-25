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

`Memory.search` turns these friendly arguments into a `MemoryQuery`: optional
text, an optional root id, one scope, optional block types and filters, and a
limit that cannot be negative. Backends return ranked `SearchResult` values.
Each one holds a block, a finite numeric score, and a reason. Building a query
checks the runtime types, and it copies the type lists and nested filter
operands that the caller owns.

The limit bounds what a backend returns, not what the caller receives. The
backend applies the limit first. Then `Memory` removes results that the
principal cannot see and results outside the query scope. So a search can return
fewer results than the limit even when more matching blocks exist. There is no
result cursor. A caller who needs a full page asks for a larger limit and cuts
the list itself, and decides how much extra backend work that is worth.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | Blocks linked to the root |
| `global` | All blocks the backend can see |

Every scope except `global` needs a root when you search through `Memory`.

Each scope is resolved from stored state, not from the query: `subtree` and
`ancestors` come from stored parents, and `links` comes from stored links. A
search index that holds neither one needs traversal and link access to resolve
them.

No scope resolves structure membership. Key bindings, log entries, temporal
entries, and revisions record membership by block id and do not change stored
parents. So a search rooted at a container block finds only the members that the
application also created as children of that container.

A bounded handle cannot use search to escape its root. It also cannot use search
to turn on global or ancestor access that the handle has disabled.

## Filters

A filter addresses a block field or a metadata field such as
`metadata.priority`. A name with no prefix matches a declared `MemoryBlock`
field first, then metadata. `$in` needs a list-like collection. An invalid or
empty operator map fails closed. Operators are normalized like this:

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

`InMemoryBackend`, `SQLiteBackend`, and `Neo4jBackend` all use the same portable
matcher. The persistent adapters scan candidates inside the requested scope.
Other adapters may use richer indexes, as long as the public meaning of a query
stays the same.

## Authorization flow

1. Genie checks the requested root and scope.
2. `before_search` may change the normalized query.
3. Genie authorizes the changed query again.
4. The backend searches.
5. Genie decodes the results and filters them by policy.
6. `after_search` may filter or reorder them.
7. Genie applies the scope and policy filters one more time.

This is what stops a hook or a backend result from widening access.

Related: [Memory](../memory/README.md), [Backends](../backend/README.md),
[Policies](../policy/README.md), and [Hooks](../hooks/README.md).
