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

A **route** is one named backend in that mapping, such as `us` or `eu`.

## Routing rules

- **One route per block.** A tree never spans two routes.
- **A child is always stored in its parent's route,** and a block cannot be moved to a new parent in another route. Both attempts raise `MemoryConflict`.
- **That rule is what makes route-local subtree traversal and deletion exact.**
- **Routes can be registered later,** which suits applications that discover tenants or mounts at runtime.
- **`routes` is a read-only live view.** A route is replaced only through `register_route`, which refuses to replace a route whose backend still holds blocks. That check asks the backend directly instead of trusting the internal catalog, because the catalog fills in lazily and is empty when a composite is opened over a store that already holds data.

```python
owner = backend.backend_for_block(block.id)
```

- **`backend_for_block` returns the adapter that owns a block,** and every route-local operation goes through it.
- **A container's subtree is covered too,** because a tree never spans routes.
- **An unknown id returns `None`.**

## Shared components

- **Optional.** Shared traversal, search, and link components can all be left out.
- **Without a shared link store,** links across routes are rejected.
- **Mounts do not change ownership,** and mount discovery neither enters nor indexes a target route.
- **A shared link store is a store,** so `RoutedBackend` sends every link write, read, and cleanup into it.
- **A shared traversal or search index is only read.** Refreshing it after each committed write is the application's job, usually from a hook.
- **A shared traversal index answers traversal reads from one place** instead of resolving a route on every call. It does not change where a block is placed, so it does not let a parent and a child sit in different routes.

## Structure facades

- **`RoutedBackend` owns no structural state,** so it implements the core contract and none of the optional structure capabilities.
- **It does implement `BlockRouter`.** Pass the composite as the facade's store, and it resolves to the route that owns the container block.

```python
from genie.structures import KeyedCollection


backend = RoutedBackend({"a": sql_a, "b": sql_b}, default_route="a")
memory = Memory(backend=backend)
catalog_root = memory.put(type="collection", value="catalog")

catalog = KeyedCollection(memory, backend, collection=catalog_root)
```

- **Resolution happens once,** when the facade is created. The container is fixed at that moment, and a container and its subtree always share one route, so routing on every call would return the same adapter every time.
- **You can still pass a route directly.** That is how you say explicitly which adapter holds the records.
- **Reads and writes still go through `memory`,** so routing, policy, and hooks are unchanged. Only the structure records live in the resolved route.
- **`RoutedBackend` gains no structure method from this.** It routes, and the facade binds to the adapter that stores.
- **Membership stays inside that route.** Binding a block owned by another route raises `MemoryConflict`, because that route's structure store does not hold the block. This is the same limit that links have when there is no shared link store.
- **To span routes** you need a capability-aware composite or a shared structure store.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
