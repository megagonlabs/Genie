# Basic examples

These scripts introduce one core feature at a time. Together they cover the
main application-facing API without requiring an agent framework or external
service:

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `crud_blocks.py` | `Memory`, `MemoryBlock` | CRUD, dictionary/metadata updates, versions, ownership traversal, recursive deletion |
| `block_presence.py` | `Memory.contains`, `MemoryNotFound` | Subtree membership at any depth, reportable absence versus operations that must raise |
| `search_filters.py` | `MemoryQuery`, `Scope`, `SearchResult` | Friendly and explicit queries, normalized filters, scoped/type-filtered search, scores and reasons |
| `scoped_agents.py` | `MemoryHandle` | Bounded and nested handles, deliberate global access, scope enforcement |
| `links.py` | `MemoryLink`, `LinkRelations` | Typed links, metadata updates, link search, symbolic mount discovery and lifecycle |
| `acyclic_links.py` | `Memory.add_acyclic_link` | Atomic cycle-checked inserts, per-relation cycle detection, reserved mount relation |
| `mounted_handles.py` | `MemoryHandle` mount capability | Mount access as an opt-in capability, explicit resolution outside the subtree, no sub-handle escalation |
| `policies.py` | `Principal`, `MemoryPolicy`, `AccessRule`, `PolicyEngine` | Private defaults, principal/role sharing, custom operations, block overrides |
| `hook_audit.py` | `MemoryHook`, `HookRegistry`, `HookContext`, `Traversal`, `Suppression` | Scopes, traversal and terminal events, refusals, mandatory hooks, suppression windows, fail-open sinks |
| `write_hooks.py` | `MemoryHook` write callbacks | Transforming versus observing callbacks, paired before/after values, refusing an operation, link metadata-only transforms |
| `search_hooks.py` | `MemoryHook`, `MemoryQuery`, `SearchResult` | Query rewriting before a search runs, result review before the caller sees it, fail-fast transforms |
| `custom_codecs.py` | `MemoryCodec`, `CodecRegistry`, `EncodedValue` | Implementing a codec, registration order, stored payloads and content types, late registration |
| `stdlib_codecs.py` | Standard-library codecs | Explicit bytes, datetime, decimal, UUID, dataclass, and enum registration |
| `routed_backend.py` | `RoutedBackend`, `InMemoryLinkStore` | Route selection/inheritance, cross-store search, pending mount resolution |
| `sqlite_persistence.py` | `SQLiteBackend` | Persistent blocks and links across backend instances |
| `heterogeneous_backend.py` | `RoutedBackend`, `SQLiteBackend`, `TraversalIndex`/`SearchIndex`/`LinkStore` | One backend assembled from SQL blocks, a graph topology index, and a vector search index; resolving query scope inside a search index; keeping derived indexes current from a hook |
| `basic_usage.py` | Core components together | Integrated application-owned memory and bounded agent tools |

`hook_audit.py`, `write_hooks.py`, and `search_hooks.py` cover the hook
callbacks the core package emits. The remaining pair,
`before_structure_change` and `after_structure_change`, is reported only by
optional `genie.structures` facades, so it is covered in
[`advanced/structure_hooks.py`](../advanced/structure_hooks.py).

Every script contains assertions. Most use `InMemoryBackend`; the storage
examples deliberately use routed stores, SQLite, or application-supplied
traversal and search components. Run commands and shared
conventions are in the [examples guide](../README.md).
