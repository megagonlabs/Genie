# Lamps

Lamps are runnable application-level memory patterns composed from Genie.
Some are inspired by other systems, but they are architectural examples—not
compatibility layers or production implementations.

Each lamp preserves the represented project's primary memory abstraction and
one characteristic workflow. It may replace model calls, specialized indexes,
or production storage with deterministic application code so the example runs
locally. Any extra Genie structure is labeled as an extension rather than an
upstream feature. The mappings below were checked against the linked official
projects and documentation on 2026-08-13.

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

| Lamp | Preserved upstream idea | Characteristic workflow |
| --- | --- | --- |
| [Graphiti](graphiti/README.md) | Episodes, entity nodes with evolving summaries, bi-temporal relationship facts, communities | Ingest episodes, invalidate contradicted facts on both time axes, group entities by slice, and rank facts with a hybrid recipe |
| [Hindsight](hindsight/README.md) | World/experience facts, derived observations, four-way retrieval, mental models | Retain, recall, and assemble a deterministic stand-in for reflection |
| [LangMem](langmem/README.md) | Schema-shaped values at namespace/key addresses | Agent-managed create/update/delete plus direct and relevant-memory retrieval |
| [Mem0](mem0/README.md) | Extracted memories scoped by user/agent/run identifiers | Add, update, and retrieve compact memories; deterministic extraction substitutes for an LLM |
| [MemoryOS](memoryos/README.md) | Short-term FIFO, mid-term sessions/pages, heat, profile and knowledge stores | Overflow, heat relevant sessions, and consolidate hot context into long-term memory |
