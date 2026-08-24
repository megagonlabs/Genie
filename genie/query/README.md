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
block, finite numeric score, and reason. Query construction validates runtime
types and copies caller-owned type lists and nested filter operands.

The limit bounds what a backend returns, not what a caller receives. A backend
applies it before `Memory` removes results the principal cannot see and results
outside the query scope, so a search can return fewer results than the limit
even when more matching blocks exist. There is no result cursor: a caller that
needs a full page requests a larger limit and truncates the result itself, and
decides how much extra backend work that is worth.

| Scope | Search area |
| --- | --- |
| `self` | Root only |
| `subtree` | Root and descendants |
| `ancestors` | Root and ancestors |
| `links` | Blocks linked to the root |
| `global` | All backend-visible blocks |

Scopes other than `global` require a root when searching through `Memory`.

Each scope is resolved from stored state rather than from the query: `subtree`
and `ancestors` from stored parents, and `links` from stored links. A search
index that holds neither needs traversal and link access to resolve them.

A bounded handle cannot use search to escape its root or enable global or
ancestor access that the handle disables.

## Filters

Filters address block fields or metadata such as `metadata.priority`.
Unprefixed names use declared `MemoryBlock` fields first, then metadata. `$in`
requires a list-like collection; invalid or empty operator maps fail closed.
Operators normalize as follows:

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>`, `>=` | `$gt`, `$gte` |
| `<`, `<=` | `$lt`, `$lte` |
| `in` | `$in` |

`InMemoryBackend`, `SQLiteBackend`, and `Neo4jBackend` use the same portable
matcher. Persistent adapters scan candidates in the requested scope. Other
adapters may use richer indexes while preserving public query meaning.

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
