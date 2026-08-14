# Basic examples

These scripts introduce one core feature at a time. Together they cover the
main application-facing API without requiring an agent framework or external
service:

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `crud_blocks.py` | `Memory`, `MemoryBlock` | CRUD, dictionary/metadata updates, versions, ownership traversal, recursive deletion |
| `search_filters.py` | `MemoryQuery`, `Scope`, `SearchResult` | Friendly and explicit queries, normalized filters, scoped/type-filtered search, scores and reasons |
| `scoped_agents.py` | `MemoryHandle` | Bounded and nested handles, deliberate global access, scope enforcement |
| `links.py` | `MemoryLink`, `LinkRelations` | Typed links, metadata updates, link search, symbolic mount discovery and lifecycle |
| `policies.py` | `Principal`, `MemoryPolicy`, `AccessRule`, `PolicyEngine` | Private defaults, principal/role sharing, custom operations, block overrides |
| `hooks_codecs.py` | `MemoryHook`, `HookContext`, `MemoryCodec`, `CodecRegistry` | Transforming/observing hooks, custom values, encoded storage payloads |
| `stdlib_codecs.py` | Standard-library codecs | Explicit bytes, datetime, decimal, UUID, dataclass, and enum registration |
| `routed_backend.py` | `RoutedBackend`, `InMemoryLinkStore` | Route selection/inheritance, cross-store search, pending mount resolution |
| `sqlite_persistence.py` | `SQLiteBackend` | Persistent blocks and links across backend instances |
| `basic_usage.py` | Core components together | Integrated application-owned memory and bounded agent tools |

Every script contains assertions. Most use `InMemoryBackend`; the storage
examples deliberately use routed stores or SQLite. Run commands and shared
conventions are in the [examples guide](../README.md).
