# Composite backend

Composite adapters coordinate other backends; they are not database types.
`RoutedBackend` presents several `MemoryBackend` instances as one, with optional
`InMemoryLinkStore` for shared links.

```python
from genie import InMemoryBackend, RoutedBackend


backend = RoutedBackend(
    {"us": InMemoryBackend(), "eu": InMemoryBackend()},
    default_route="us",
)
```

Each block has one owning route, and a tree never spans routes. A child is
always stored in its parent's route, and a block cannot be reparented into
another route; both attempts raise `MemoryConflict`. That invariant is what
makes route-local subtree traversal and deletion exact.

Routes may be registered later for application-managed tenant or mount
discovery. `routes` is a read-only live view, so a route is replaced only
through `register_route`, which refuses to replace one whose backend still
holds blocks. That check asks the backend directly rather than trusting the
internal catalog, because the catalog fills lazily and is empty for a composite
opened over a store that already has data.

`backend_for_block` resolves the adapter that owns a block, and is the routing
primitive every route-local operation goes through:

```python
owner = backend.backend_for_block(block.id)
```

Because a tree never spans routes, the adapter returned for a container block
also owns every block in that container's subtree. It returns `None` for an
unknown id.

Shared traversal, search, or link components are optional. Without a shared
link store, cross-route links are rejected. Mounts do not change ownership, and
mount discovery does not enter or index a target route.

Shared components follow the store and index rule. `RoutedBackend` routes every
link write, read, and cleanup into a shared link store, because a link store is
a store. It only reads a shared traversal or search index, so refreshing those
after each committed write is application work, usually from a hook. A shared
traversal index serves traversal reads from one place instead of resolving a
route per call; it does not change ownership placement, so it does not let a
parent and child sit in different routes.

## Structure facades

`RoutedBackend` owns no structural state, so it implements the core contract and
none of the optional structure capabilities. It does implement `BlockRouter`, so
pass the composite as the facade's store and it resolves to the route that owns
the container block:

```python
from genie.structures import KeyedCollection


backend = RoutedBackend({"a": sql_a, "b": sql_b}, default_route="a")
memory = Memory(backend=backend)
catalog_root = memory.put(type="collection", value="catalog")

catalog = KeyedCollection(memory, backend, collection=catalog_root)
```

Resolution happens once, when the facade is created. A container is fixed at that
point and a container and its subtree always share one route, so per-call routing
would return the same adapter every time. Passing a route directly still works and
is the way to be explicit about which adapter holds the records.

Reads and writes go through `memory`, so routing, policy, and hooks are unchanged;
only the structure records live in the resolved route. `RoutedBackend` gains no
structure method from this: it routes, and the facade binds to the adapter that
stores.

Membership stays inside that route. Binding a block owned by another route
raises `MemoryConflict`, because the route's structure store does not hold the
block. This is the same constraint links have without a shared link store.
Spanning routes needs a capability-aware composite or a shared structure store.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
