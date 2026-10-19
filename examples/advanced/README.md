# Advanced structure examples

These scripts combine optional facades. The facades do not change the values
that the blocks store.

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `temporal_collection.py` | `TemporalCollection`, `ValidityInterval`, temporal records | Event ranges, validity at a point in time, overlap, combined filters, replacement, and membership removal |
| `graph_traversal.py` | `LineageDAG`, `PropertyGraph`, `GraphNeighbor`, `GraphPath` | One-hop neighbors, multi-hop walks in both directions, depth limits, and edge annotation |
| `versioned_catalog.py` | `VersionedRecords`, `OwnershipTree`, `MemoryAddress` | Exact and recursive identity listings, revision history, subtree pruning, and facade root boundaries |
| `agent_memory.py` | `OwnershipTree`, `KeyedCollection`, `EventLog`, `PropertyGraph` | Application-owned value views over generic facades, backend capability checks, and agent decisions outside memory |
| `experiment_memory.py` | All 7 facades and the records that they return | Tree traversal, graph paths, namespaced keys, log positions, time, cycle-safe lineage, record revisions, and one block in several structures |
| `facade_containers.py` | Facade container accessors, `KeyedItem` | The block that a facade binds to, a live block property versus a stable id, bindings versus blocks, deleted containers |
| `structure_hooks.py` | `StructureChange`, `StructureKind`, `StructureAction`, `MemoryHook` | Each structural action, proposed versus committed records, refused changes as terminal events, and composed facades that reuse events |

- **`structure_hooks.py` audits the same facades from the outside.** Explicit key, log, and temporal changes report `before_structure_change` and `after_structure_change` to the registry of the access object. So one trail records how the application organized memory, next to what memory stored. Reads of these facades report block read (`get`) events, not structural events. A rebind that the facade refuses reports only its terminal event.
- **`experiment_memory.py` covers all 7 facades:** `OwnershipTree`, `PropertyGraph`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, and `VersionedRecords`. The facades after `PropertyGraph` use the optional `KeyedStore`, `SequenceStore`, `TemporalStore`, `TemporalIndex`, and `AcyclicLinkStore` capabilities. `InMemoryBackend` implements these capabilities.
- **Each script uses `InMemoryBackend` and assertions.**
- **The meaning of application values and the behavior of agents stay outside Genie.**
- **Run commands and shared conventions** are in the [examples guide](../README.md).
