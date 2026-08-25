# Basic examples

Each script introduces one core feature. Together they cover the main
application-facing API, and none of them needs an agent framework or an external
service:

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `crud_blocks.py` | `Memory`, `MemoryBlock` | CRUD, dictionary/metadata updates, versions, ownership traversal, recursive deletion |
| `block_presence.py` | `Memory.contains`, `MemoryNotFound` | Subtree membership at any depth, absence you can report versus operations that must raise |
| `search_filters.py` | `MemoryQuery`, `Scope`, `SearchResult` | Friendly and explicit queries, normalized filters, search by scope and by type, scores and reasons |
| `scoped_agents.py` | `MemoryHandle` | Bounded and nested handles, deliberate global access, scope enforcement |
| `links.py` | `MemoryLink`, `LinkRelations` | Typed links, metadata updates, link search, symbolic mount discovery and lifecycle |
| `acyclic_links.py` | `Memory.add_acyclic_link` | Atomic cycle-checked inserts, cycle detection per relation, the reserved mount relation |
| `mounted_handles.py` | `MemoryHandle` mount capability | Mount access as an opt-in capability, explicit resolution outside the subtree, no escalation through sub-handles |
| `policies.py` | `Principal`, `MemoryPolicy`, `AccessRule`, `PolicyEngine` | Private defaults, sharing by principal and role, custom operations, block overrides |
| `hook_audit.py` | `MemoryHook`, `HookRegistry`, `HookContext`, `Traversal`, `Suppression` | Scopes, traversal and terminal events, refusals, mandatory hooks, suppression windows, fail-open sinks |
| `write_hooks.py` | `MemoryHook` write callbacks | Transforming versus observing callbacks, paired before/after values, refusing an operation, link transforms limited to metadata |
| `search_hooks.py` | `MemoryHook`, `MemoryQuery`, `SearchResult` | Rewriting a query before the search runs, reviewing results before the caller sees them, fail-fast transforms |
| `custom_codecs.py` | `MemoryCodec`, `CodecRegistry`, `EncodedValue` | Writing a codec, registration order, stored payloads and content types, late registration |
| `stdlib_codecs.py` | Standard-library codecs | Explicit registration of bytes, datetime, decimal, UUID, dataclass, and enum codecs |
| `routed_backend.py` | `RoutedBackend`, `InMemoryLinkStore` | Choosing and inheriting a route, search across stores, resolving a pending mount |
| `sqlite_persistence.py` | `SQLiteBackend` | Blocks and links that survive across backend instances |
| `heterogeneous_backend.py` | `RoutedBackend`, `SQLiteBackend`, `TraversalIndex`/`SearchIndex`/`LinkStore` | One backend assembled from SQL blocks, a graph topology index, and a vector search index; resolving query scope inside a search index; keeping derived indexes current from a hook |
| `basic_usage.py` | Core components together | Application-owned memory and bounded agent tools, working together |

`hook_audit.py`, `write_hooks.py`, and `search_hooks.py` cover the hook
callbacks that the core package sends. The last pair,
`before_structure_change` and `after_structure_change`, is sent only by the
optional `genie.structures` facades, so it is covered in
[`advanced/structure_hooks.py`](../advanced/structure_hooks.py).

Every script contains assertions. Most use `InMemoryBackend`. The storage
examples use routed stores, SQLite, or traversal and search components supplied
by the application, on purpose. Run commands and shared conventions are in the
[examples guide](../README.md).
