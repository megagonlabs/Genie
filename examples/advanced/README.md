# Advanced structure examples

These scripts compose optional facades without changing stored block values:

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `temporal_collection.py` | `TemporalCollection`, `ValidityInterval`, temporal records | Event ranges, point-in-time validity, overlap, combined filters, replacement, and membership removal |
| `graph_traversal.py` | `LineageDAG`, `PropertyGraph`, `GraphNeighbor`, `GraphPath` | One-hop neighbors, bounded multi-hop walks in both directions, depth limits, and edge annotation |
| `versioned_catalog.py` | `VersionedRecords`, `OwnershipTree`, `MemoryAddress` | Exact and recursive identity listings, revision history, subtree pruning, and facade root boundaries |
| `agent_memory.py` | `OwnershipTree`, `KeyedCollection`, `EventLog`, `PropertyGraph` | Application-owned value views over generic facades; backend capability checks; agent decisions outside memory |
| `experiment_memory.py` | All seven facades and their returned records | Tree traversal, graph paths, namespaced keys, log positions, time, cycle-safe lineage, record revisions, and block reuse |
| `facade_containers.py` | Facade container accessors, `KeyedItem` | The block a facade is bound to, live re-reads versus stable ids, bindings versus blocks, deleted containers |
| `structure_hooks.py` | `StructureChange`, `StructureKind`, `StructureAction`, `MemoryHook` | Every structural action, proposed versus committed records, refused changes as terminal events, and composed facades reusing events |

`structure_hooks.py` audits the same facades from the outside. Explicit key,
log, and temporal mutations report `before_structure_change` and
`after_structure_change` to the access object's registry, so one trail records
how memory was organized alongside what it stored. Reads report traversal
events instead, and a refused membership change reports only its terminal
event.

`experiment_memory.py` covers `OwnershipTree`, `PropertyGraph`,
`KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, and
`VersionedRecords`. The latter facades exercise the optional `KeyedStore`,
`SequenceStore`, `TemporalStore`, `TemporalIndex`, and `AcyclicLinkStore`
capabilities implemented by `InMemoryBackend`.

Every script uses `InMemoryBackend` and assertions. Application value meaning
and agent behavior remain outside Genie. Run commands and shared conventions
are in the [examples guide](../README.md).
