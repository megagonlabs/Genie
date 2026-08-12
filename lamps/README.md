# Lamps

Lamps are runnable application-level memory patterns composed from Genie.
Some are inspired by other systems, but they are architectural examples—not
compatibility layers or production implementations.

Each `lamps/<name>` package should:

- explain its model mapping and deliberate limits;
- keep taxonomy, retrieval, agent behavior, and other business logic outside
  Genie core;
- use only the structure facades that fit its model;
- run from the repository root without hidden or nondeterministic state; and
- keep tests under `lamps/<name>/tests`.

## Run and test

```bash
conda run -n "genie" python -m lamps.<name>
conda run -n "genie" python -m pytest lamps/<name>/tests -q
```

Pytest also discovers all lamp tests through `pyproject.toml`.

## Shared agent-demo format

Agent lamps are deterministic and require no API key or agent framework. Their
named tools use JSON-compatible inputs and results, so a model-backed agent
could use the same boundary. Application code chooses the tool calls; Genie
only supplies memory access and structures.

Terminal output follows one format: title and purpose, Genie structures, tool
definitions, agent answer, ordered calls, and per-tool counts. Individual lamp
guides document only their unique stored model, workflow, results, and limits.

## Included lamps

- [Graphiti](graphiti/README.md): temporal knowledge graph and provenance.
- [Hindsight](hindsight/README.md): atomic units, mental models, and combined
  retrieval.
- [LangMem](langmem/README.md): schema-defined namespace/key/value items.
- [Mem0](mem0/README.md): scoped extracted-fact records.
- [MemoryOS](memoryos/README.md): short-, mid-, and long-term hierarchy.
