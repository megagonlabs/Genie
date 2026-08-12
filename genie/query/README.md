# Queries

`genie.query` defines portable search requests and results without choosing a
ranking method or search engine.

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

`Memory.search` normalizes friendly arguments into `MemoryQuery`: optional
text and root id, one scope, optional block types and filters, and a
non-negative limit. Backends return ranked `SearchResult` values containing a
block, score, and reason.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | Blocks linked to the root |
| `global` | All backend-visible blocks |

A bounded handle cannot use search to escape its root or enable global or
ancestor access that the handle disables.

## Filters

Filters address block fields or metadata such as `metadata.priority`.
Operators normalize as follows:

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

`InMemoryBackend` and `SQLiteBackend` use the same portable matcher; SQLite
scans candidates in the requested scope. Other adapters may use richer indexes
while preserving public query meaning.

## Authorization flow

1. Genie checks the requested root and scope.
2. `before_search` may modify the normalized query.
3. Genie authorizes the modified query again.
4. The backend searches.
5. Genie decodes and policy-filters results.
6. `after_search` may filter or reorder them.
7. Genie reapplies scope and policy filtering.

This prevents hooks or backend results from widening access.

Related: [Memory](../memory/README.md), [Backends](../backend/README.md),
[Policies](../policy/README.md), and [Hooks](../hooks/README.md).
