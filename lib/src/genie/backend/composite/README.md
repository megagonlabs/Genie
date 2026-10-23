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
- **A route that the instance does not have raises `UnsupportedOperation`.** This applies to a route that the route selector chooses for a new root block, and to a route that the route catalog names for an id. The call succeeds after `register_route` adds that route. A put to an unknown route claims nothing in the route catalog.
- **That rule makes route-local subtree traversal and delete exact.**
- **An id in 2 routes raises `BackendCorruption`** on each lookup, also on `get`. The stored data breaks the one-route rule, so the error is not a `MemoryConflict`, and a retry cannot help. The composite does not choose one of the copies. The application removes the wrong copy through the backend of its route.
- **Routes can be registered later.** This suits applications that find tenants or mounts at runtime.
- **Each instance has a route cache.** It records the route of each block id that the instance finds. A cached route counts only while that route still holds the block, because another instance can delete the block and store the id again in a different route. So each routed call that uses the cache reads the block once in the cached route.
- **`routes` is a read-only live view.** A route replacement goes only through `register_route`. It refuses to replace a route whose backend still holds blocks. The check uses the route cache, and it also asks the backend directly, because the cache fills lazily. The cache is empty when a composite opens over a store that already holds data. If the backend search raises `UnsupportedOperation`, the route counts as empty.

```python
owner = backend.backend_for_block(block.id)
```

- **`backend_for_block` returns the adapter that owns a block,** and each route-local operation goes through it.
- **A container's subtree is covered too,** because a tree never spans routes.
- **An unknown id returns `None`.**

## Route catalog

- **Without a route catalog, ids stay unique across routes only inside one instance.** A put checks that no route holds the id, and then it writes to the selected route. Only the lock of the instance protects these 2 steps. So 2 instances over the same routes, for example one in each worker process, can store one id in 2 routes. After that, each lookup of the id raises `BackendCorruption`.
- **`route_catalog` gives each id one route across all instances.** Give each instance the same `RouteCatalog`. A put claims the selected route for the new id in one atomic step, and then it writes the block. A second claim for the id raises `MemoryConflict`, also from another instance or process.
- **Adapters that implement `RouteCatalog`:** `InMemoryBackend`, `SQLiteBackend`, `PostgresBackend`, `MongoDBBackend`, `RedisBackend`, and `Neo4jBackend`. `InMemoryBackend` serves only one process. Several processes use a database that all of them reach.
- **A route can hold the catalog.** Catalog entries are not blocks, so they do not share the id space of the blocks. `route_catalog` can be one of the routes or a separate database.

```python
from genie import RoutedBackend, SQLiteBackend


backend = RoutedBackend(
    {"us": SQLiteBackend("us.db"), "eu": SQLiteBackend("eu.db")},
    default_route="us",
    route_catalog=SQLiteBackend("routes.db"),
)
```

- **Lookup order.** The route cache comes first. Then the catalog entry decides the route. An id with no entry is looked up in each route, which finds blocks that were stored before the catalog was in use.
- **An entry that names a route the instance does not have** raises `UnsupportedOperation`. It does not read as a missing block. This occurs, for example, when another instance added the route with `register_route`. The read succeeds after this instance registers the same route.
- **A failed write releases its claim** if the route does not hold the block. The claim stays if the route holds the block, or if that check fails, because a backend error can hide a write that committed.
- **A delete releases the entries** of all deleted blocks after the route commits the delete. If the release fails, the delete still succeeds, and a warning goes to the `genie.backend.composite.routed` logger.
- **An entry with no block** reads as no block, and a put of its id raises `MemoryConflict`. A crash between the claim and the write, or a failed release, leaves such an entry. It never lets an id get a second route. `route_catalog.release_routes([block_id])` removes it when no write of that id is in progress.
- **All instances use the same catalog.** An instance without it, or with a different catalog, does not claim ids, so its writes do not get the guarantee.
- **Cost.** A put does one atomic claim in the catalog. A lookup that misses the route cache reads the catalog entry before it reads the route.

## Shared components

- **Optional.** Shared traversal, search, and link components are all optional.
- **Without a shared link store,** links across routes are rejected.
- **Mounts do not change ownership.** Mount discovery does not go into a target route, and it does not index it.
- **A shared link store is a store.** So `RoutedBackend` sends all link writes, reads, and cleanups to it.
- **`RoutedBackend` only reads a shared traversal or search index.** The application refreshes it after each committed write, usually from a hook.
- **A global search without a shared search index compares raw scores across routes.** Each route returns up to `limit` results after `query.after`. `RoutedBackend` sorts them together in the search order and keeps the first `limit`. It does not normalize or fuse scores. So the order has a meaning only if all routes use the same score scale. For routes with different scales, give a `search_index` that scores all blocks with one method.
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
