# Composite backend

A composite adapter coordinates other backends. It is not a type of database.
`RoutedBackend` shows several `MemoryBackend` instances as one, and it can take
an optional `InMemoryLinkStore` for shared links.

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
- **A child is always stored in the route of its parent,** and a block cannot move to a new parent in another route. Both attempts raise `MemoryConflict`.
- **That rule makes route-local subtree traversal and delete exact.**
- **Routes can be registered later.** This suits applications that find tenants or mounts at runtime.
- **`routes` is a read-only live view.** A route replacement goes only through `register_route`. It refuses to replace a route whose backend still holds blocks. The check uses the internal catalog, and it also asks the backend directly, because the catalog fills lazily. The catalog is empty when a composite opens over a store that already holds data. If the backend search raises `UnsupportedOperation`, the route counts as empty.

```python
owner = backend.backend_for_block(block.id)
```

- **`backend_for_block` returns the adapter that owns a block,** and each route-local operation goes through it.
- **A container's subtree is covered too,** because a tree never spans routes.
- **An unknown id returns `None`.**

## Shared components

- **Optional.** Shared traversal, search, and link components are all optional.
- **Without a shared link store,** links across routes are rejected.
- **Mounts do not change ownership.** Mount discovery does not go into a target route, and it does not index it.
- **A shared link store is a store.** So `RoutedBackend` sends all link writes, reads, and cleanups to it.
- **`RoutedBackend` only reads a shared traversal or search index.** The application refreshes it after each committed write, usually from a hook.
- **A shared traversal index answers traversal reads from one place,** and no call resolves a route. It does not change where a block goes. So it does not let a parent and a child be in different routes.

## Acyclic links

- **`RoutedBackend` implements `AcyclicLinkStore`.** So `LineageDAG` and `Memory.add_acyclic_link` work over it. Links are part of the core contract, so the composite already routes them. `add_acyclic_link` goes to the same place as `add_link`.
- **Without a shared link store,** the route that owns both endpoints checks for cycles. Links never cross routes in this mode. So a cycle can form only inside one route, and the route-local check is exact.
- **A cross-route acyclic link raises `MemoryConflict`,** the same as a plain cross-route link.
- **Each route protects a relation alone.** A route rejects plain `add_link` calls for a relation only after its own first acyclic insert, not after one in another route. A relation stays acyclic in all routes only if all its writes go through `LineageDAG` or `add_acyclic_link`.
- **With a shared link store,** that store checks cycles across all routes. So it needs `AcyclicLinkStore`. `InMemoryLinkStore` does not implement it, so an acyclic insert with it raises `UnsupportedOperation`.
- **A route without `AcyclicLinkStore` raises `UnsupportedOperation`,** and it stores no link.

## Structure facades

- **`RoutedBackend` owns no structural state.** So it implements none of the record-store capabilities: `KeyedStore`, `SequenceStore`, `TemporalStore`, and `TemporalIndex`.
- **It implements `BlockRouter`.** As the store of a facade, the composite resolves to the route that owns the container block.

```python
from genie.structures import KeyedCollection


backend = RoutedBackend({"a": sql_a, "b": sql_b}, default_route="a")
memory = Memory(backend=backend)
catalog_root = memory.put(type="collection", value="catalog")

catalog = KeyedCollection(memory, backend, collection=catalog_root)
```

- **Resolution happens once,** when the facade is created. The container is fixed at that moment, and a container and its subtree always share one route. So a route lookup on each call returns the same adapter each time.
- **You can still give a route directly.** That is how you tell explicitly which adapter holds the records.
- **Reads and writes still go through `memory`.** So routing, policy, and hooks do not change. Only the structure records are in the resolved route.
- **`RoutedBackend` gets no structure method from this.** It routes, and the facade binds to the adapter that stores the records.
- **Membership stays inside that route.** A bind of a block that another route owns raises `MemoryConflict`, because the structure store of the resolved route does not hold the block. Links have the same limit when there is no shared link store.
- **Membership across routes** needs a capability-aware composite or a shared structure store.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
