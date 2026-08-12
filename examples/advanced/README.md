# Advanced structure examples

These scripts compose optional facades without changing stored block values:

| Example | Focus |
| --- | --- |
| `temporal_collection.py` | Event time and validity queries |
| `agent_memory.py` | `TaskMemory` hides facade setup behind three value-oriented views; the agent interprets values and chooses actions |
| `experiment_memory.py` | Ownership, graph, keys, logs, time, lineage, and versions in one workflow |

Every script uses `InMemoryBackend` and assertions. Application types and agent
behavior remain outside Genie. Run commands and shared conventions are in the
[examples guide](../README.md).
