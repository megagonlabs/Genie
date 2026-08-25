# Lamps

A lamp is a runnable application-level memory pattern built from Genie. Some
lamps take their ideas from other systems, but they are architectural examples.
They are not compatibility layers, and they are not production implementations.

Each lamp keeps the main memory idea of the project it represents, plus one
workflow that is typical of it. It may replace model calls, specialized indexes,
or production storage with plain application code, so that the example runs
locally. Any extra Genie structure is marked as an addition rather than an
upstream feature.

Each `lamps/<name>` package should:

- explain its model mapping and the limits it chose;
- keep taxonomy, retrieval, agent behavior, and other business logic outside
  Genie core;
- use only the structure facades that fit its model;
- run from the repository root with no hidden or random state; and
- keep its tests under `lamps/<name>/tests`.

## Run and test

Every lamp is a package, and its `demo.py` is the entry point. Run it as a
module from the repository root, so that both the `lamps` and `genie` packages
can be found:

```bash
conda run -n "genie" python -m lamps.<name>.demo
conda run -n "genie" python -m pytest lamps/<name>/tests -q
```

Put each lamp name in place of `<name>`:

| Lamp | Run the demo | Run the tests |
| --- | --- | --- |
| Graphiti | `conda run -n "genie" python -m lamps.graphiti.demo` | `conda run -n "genie" python -m pytest lamps/graphiti/tests -q` |
| Hindsight | `conda run -n "genie" python -m lamps.hindsight.demo` | `conda run -n "genie" python -m pytest lamps/hindsight/tests -q` |
| LangMem | `conda run -n "genie" python -m lamps.langmem.demo` | `conda run -n "genie" python -m pytest lamps/langmem/tests -q` |
| Mem0 | `conda run -n "genie" python -m lamps.mem0.demo` | `conda run -n "genie" python -m pytest lamps/mem0/tests -q` |
| MemoryOS | `conda run -n "genie" python -m lamps.memoryos.demo` | `conda run -n "genie" python -m pytest lamps/memoryos/tests -q` |

Each demo prints its whole run and then exits. No lamp reads a clock, a network,
or an API key, so running one again prints the same output. Pytest also finds
all lamp tests through `pyproject.toml`, so `conda run -n "genie" python -m
pytest lamps -q` runs every lamp at once.

## Shared agent-demo format

The agent lamps are deterministic, and they need no API key and no agent
framework. Their named tools take and return JSON-compatible values, so an agent
backed by a model could use the same boundary. The application code chooses the
tool calls. Genie only supplies memory access and structures.

Terminal output follows one format: title and purpose, Genie structures, tool
definitions, agent answer, ordered calls, and a count per tool. Each lamp guide
documents only its own stored model, workflow, results, and limits.

## Included lamps

| Lamp | Upstream idea it keeps | Typical workflow |
| --- | --- | --- |
| [Graphiti](graphiti/README.md) | Episodes, entity nodes with summaries that change over time, relationship facts on two time axes, communities | Ingest episodes, invalidate contradicted facts on both time axes, group entities by slice, and rank facts with a hybrid recipe |
| [Hindsight](hindsight/README.md) | Documents turned into world and experience facts, observations built up with proof counts, standing mental models, four-way retrieval | Retain and consolidate, refine a belief as evidence supports or reverses it, and answer from the highest layer that already knows |
| [LangMem](langmem/README.md) | Namespaced items in collection and profile shapes, memory formed on the hot path or in the background, the agent's prompt as memory | Debounce a thread, apply the changes the manager is allowed to make, and revise the prompt from feedback |
| [Mem0](mem0/README.md) | Facts pulled from an exchange and reconciled against what is on file, scoped by user, agent, and run identifiers, with a change history and a graph half | Decide `ADD`, `UPDATE`, `DELETE`, or `NONE` for each candidate, apply it, and read afterwards why a memory says what it says |
| [MemoryOS](memoryos/README.md) | An operating-system hierarchy: a short-term FIFO, mid-term topic sessions scored by heat, and a long-term profile beside two capped knowledge bases | Page the oldest exchange out of a full queue, let retrieval heat a session until it crosses the threshold and is read into the profile, and evict the session nobody came back to |
