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

Each block has one owning route. Children stay on their parent's route by
default, preserving subtree traversal and deletion. Routes may be registered
later for application-managed tenant or mount discovery.

Shared traversal, search, or link components are optional. Without a shared
link store, cross-route links are rejected. Mounts do not change ownership, and
mount discovery does not enter or index a target route.

`RoutedBackend` exposes the core contract. Keyed, sequence, temporal, and
acyclic capabilities require a capability-aware composite or shared store.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
