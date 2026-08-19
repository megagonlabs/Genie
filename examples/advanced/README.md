# Advanced structure examples

These scripts compose optional facades without changing stored block values:

| Example | Components | Capabilities shown |
| --- | --- | --- |
| `temporal_collection.py` | `TemporalCollection`, `ValidityInterval`, temporal records | Event ranges, point-in-time validity, overlap, replacement, and membership removal |
| `agent_memory.py` | `OwnershipTree`, `KeyedCollection`, `EventLog`, `PropertyGraph` | Application-owned value views over generic facades; backend capability checks; agent decisions outside memory |
| `experiment_memory.py` | All seven facades and their returned records | Tree traversal, graph paths, namespaced keys, log positions, time, cycle-safe lineage, record revisions, and block reuse |

`experiment_memory.py` covers `OwnershipTree`, `PropertyGraph`,
`KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, and
`VersionedRecords`. The latter facades exercise the optional `KeyedStore`,
`SequenceStore`, `TemporalStore`, `TemporalIndex`, and `AcyclicLinkStore`
capabilities implemented by `InMemoryBackend`.

Every script uses `InMemoryBackend` and assertions. Application value meaning
and agent behavior remain outside Genie. Run commands and shared conventions
are in the [examples guide](../README.md).
