# Advanced Structure Examples

These examples show how optional structures can be used alone or composed
without changing the underlying `MemoryBlock` values.

## Run

Run each script from the repository root with the project Conda environment:

```bash
conda run -n "genie" python examples/advanced/temporal_collection.py
conda run -n "genie" python examples/advanced/agent_memory.py
conda run -n "genie" python examples/advanced/experiment_memory.py
```

## Included Examples

- `temporal_collection.py` uses one structure for event-time and validity
  queries.
- `agent_memory.py` shows an application-owned `TaskMemory` that hides facade
  setup. Its three views only read and write values; the agent interprets those
  values and chooses its next action.
- `experiment_memory.py` uses ownership tree, property graph, keyed collection,
  event log, temporal collection, lineage DAG, and versioned records in one
  experiment-memory workflow.

## Conventions

- Each script uses `InMemoryBackend` for a local, dependency-free demo.
- Each script contains assertions; a successful exit confirms the expected
  behavior.
- Application-specific types and agent behavior stay in the example code.
