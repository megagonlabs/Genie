# Queries

The `genie.query` package defines portable search requests and results. It does
not choose a ranking method or search engine.

## Memory queries

`Memory.search(...)` accepts friendly arguments or a `MemoryQuery`:

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

Before reaching a backend, the request is normalized into:

- optional text;
- an optional root id;
- one search scope;
- optional block types;
- canonical filters; and
- a non-negative result limit.

The backend decides how to retrieve and rank matches. It returns
`SearchResult` values containing a block, score, and reason.

## Scopes

| Scope | Search area |
| --- | --- |
| `self` | The root block only |
| `subtree` | The root and its descendants |
| `ancestors` | The root and its ancestors |
| `links` | Blocks connected to the root by links |
| `global` | All backend-visible blocks |

`MemoryHandle` limits these scopes further. A bounded handle cannot use search
to escape its root, and disabled global or ancestor access remains disabled.

## Filters

Application-facing operators are converted to canonical backend operators:

| Friendly | Canonical |
| --- | --- |
| `=`, `==` | `$eq` |
| `!=` | `$ne` |
| `>` | `$gt` |
| `>=` | `$gte` |
| `<` | `$lt` |
| `<=` | `$lte` |
| `in` | `$in` |

Filters can reference block fields or metadata fields such as
`metadata.priority`. Backend adapters receive the normalized form.

`InMemoryBackend` provides a small portable matcher. Other adapters may use
database indexes or richer retrieval internally, but they should preserve the
public query meaning.

## Authorization

Search authorization is applied outside backend ranking:

1. Genie checks the requested root and scope.
2. A `before_search` hook may modify the normalized query.
3. Genie checks the modified query again.
4. The backend executes the query.
5. Genie decodes and policy-filters the results.
6. An `after_search` hook may filter or reorder results.
7. Genie applies scope and policy filtering again.

This keeps hooks and backend results from widening the caller's access.

## Related guides

- [Memory and handles](../memory/README.md)
- [Backends](../backend/README.md)
- [Policies](../policy/README.md)
- [Hooks](../hooks/README.md)
