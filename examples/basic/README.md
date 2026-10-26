# Basic examples

Each script shows one core feature, and together the scripts cover the main
application-facing API. No script needs an agent framework or an external
service.

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `crud_blocks.py` | `Memory`, `MemoryBlock` | CRUD, dictionary and metadata updates, versions, ownership traversal, recursive delete |
| `block_presence.py` | `Memory.contains`, `MemoryNotFound` | Subtree membership at any depth, absence as a returned value versus operations that raise `MemoryNotFound` |
| `search_filters.py` | `MemoryQuery`, `Scope`, `SearchResult` | Friendly and explicit queries, normalized filters, search by scope and by type, scores and reasons |
| `scoped_agents.py` | `MemoryHandle` | Bounded and nested handles, deliberate global access, scope enforcement |
| `links.py` | `MemoryLink`, `LinkRelations` | Typed links, metadata updates, link search, symbolic mount discovery and lifecycle |
| `acyclic_links.py` | `Memory.protect_relation`, `Memory.add_acyclic_link` | Relation protection, atomic inserts that check for cycles, cycle detection per relation, the reserved mount relation |
| `mounted_handles.py` | `MemoryHandle` `allow_mounts` | `allow_mounts` as an opt-in capability, explicit resolution outside the subtree, no escalation through sub-handles |
| `policies.py` | `Principal`, `MemoryPolicy`, `AccessRule`, `PolicyEngine` | The `MemoryPolicy.owner_private()` default, shared access by principal and by role, custom operations, block overrides |
| `hook_audit.py` | `MemoryHook`, `HookRegistry`, `HookContext`, `Traversal`, `Suppression` | Scopes, traversal and terminal events, refusals, mandatory hooks, suppression windows, fail-open sinks |
| `write_hooks.py` | `MemoryHook` write callbacks | Callbacks that transform versus callbacks that observe, paired before and after values, refusal of an operation, link transforms limited to metadata |
| `search_hooks.py` | `MemoryHook`, `MemoryQuery`, `SearchResult` | Query rewrite before the search runs, result review before the caller gets the results, fail-fast transforms |
| `custom_codecs.py` | `MemoryCodec`, `CodecRegistry`, `EncodedValue` | A custom codec, registration order, stored payloads and content types, late registration |
| `stdlib_codecs.py` | Standard-library codecs | Explicit registration of bytes, datetime, decimal, UUID, dataclass, and enum codecs |
| `routed_backend.py` | `RoutedBackend`, `InMemoryLinkStore` | Route selection and route inheritance, search across stores, resolution of a pending mount |
| `routed_catalog.py` | `RoutedBackend`, `RouteCatalog`, `SQLiteBackend` | One route for each id across workers that share a route catalog, a block that another worker moved to another route |
| `sqlite_persistence.py` | `SQLiteBackend` | Blocks and links that stay available across backend instances |
| `heterogeneous_backend.py` | `RoutedBackend`, `SQLiteBackend`, `TraversalIndex`/`SearchIndex`/`LinkStore` | One backend made from SQL blocks, a graph topology index, and a vector search index. Query scope resolution inside a search index. A hook that keeps derived indexes current |
| `basic_usage.py` | Core components together | Application-owned memory and bounded agent tools that work together |

- **Core hook callbacks:** `hook_audit.py`, `write_hooks.py`, and `search_hooks.py` cover them.
- **The structural pair** `before_structure_change` and `after_structure_change` comes only from the optional `genie.structures` facades. So [`advanced/structure_hooks.py`](../advanced/structure_hooks.py) covers it.
- **Each script contains assertions,** and most scripts use `InMemoryBackend`.
- **The storage examples are different on purpose.** They use routed stores, SQLite, or traversal and search components that the application supplies.
- **Run commands and shared conventions** are in the [examples guide](../README.md).
