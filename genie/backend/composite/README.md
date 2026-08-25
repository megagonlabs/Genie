# Composite backend

A composite adapter coordinates other backends. It is not a type of database.
`RoutedBackend` presents several `MemoryBackend` instances as one, and it can
take an optional `InMemoryLinkStore` for shared links.

```python
from genie import InMemoryBackend, RoutedBackend


backend = RoutedBackend(
    {"us": InMemoryBackend(), "eu": InMemoryBackend()},
    default_route="us",
)
```

Each block belongs to exactly one route, and a tree never spans two routes. A
child is always stored in its parent's route, and a block cannot be moved to a
new parent in another route. Both attempts raise `MemoryConflict`. That rule is
what makes route-local subtree traversal and deletion exact.

You can register routes later, which suits applications that discover tenants or
mounts at runtime. `routes` is a read-only live view. A route is replaced only
through `register_route`, which refuses to replace a route whose backend still
holds blocks. That check asks the backend directly instead of trusting the
internal catalog, because the catalog fills in lazily and is empty when a
composite is opened over a store that already holds data.

`backend_for_block` returns the adapter that owns a block. Every route-local
operation goes through it:

```python
owner = backend.backend_for_block(block.id)
```

A tree never spans routes, so the adapter returned for a container block also
owns every block in that container's subtree. The method returns `None` for an
unknown id.

Shared traversal, search, and link components are optional. Without a shared
link store, links across routes are rejected. Mounts do not change ownership,
and mount discovery neither enters nor indexes a target route.

Shared components follow the store and index rule. `RoutedBackend` sends every
link write, read, and cleanup into a shared link store, because a link store is
a store. It only reads a shared traversal or search index, so refreshing those
after each committed write is the application's job, usually from a hook. A
shared traversal index answers traversal reads from one place instead of
resolving a route on every call. It does not change where a block is placed, so
it does not let a parent and a child sit in different routes.

## Structure facades

`RoutedBackend` owns no structural state. So it implements the core contract and
none of the optional structure capabilities. It does implement `BlockRouter`.
Pass the composite as the facade's store, and it resolves to the route that owns
the container block:

```python
from genie.structures import KeyedCollection


backend = RoutedBackend({"a": sql_a, "b": sql_b}, default_route="a")
memory = Memory(backend=backend)
catalog_root = memory.put(type="collection", value="catalog")

catalog = KeyedCollection(memory, backend, collection=catalog_root)
```

Resolution happens once, when the facade is created. The container is fixed at
that moment, and a container and its subtree always share one route, so routing
on every call would return the same adapter every time. You can still pass a
route directly. That is how you say explicitly which adapter holds the records.

Reads and writes still go through `memory`, so routing, policy, and hooks are
unchanged. Only the structure records live in the resolved route.
`RoutedBackend` gains no structure method from this: it routes, and the facade
binds to the adapter that stores.

Membership stays inside that route. Binding a block owned by another route
raises `MemoryConflict`, because that route's structure store does not hold the
block. This is the same limit that links have when there is no shared link
store. To span routes you need a capability-aware composite or a shared
structure store.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
