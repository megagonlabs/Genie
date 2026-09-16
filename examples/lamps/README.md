# Lamps

A lamp is a runnable application-level memory pattern built from Genie.
Smaller single-file examples are in the [examples guide](../README.md).

- **Architectural examples, not products.** Some lamps take their ideas from other systems. They are not compatibility layers, and they are not production implementations.
- **Each lamp keeps the main memory idea** of the project it represents, plus one workflow that is typical of it. These guides call that original project *upstream*.
- **Model calls, specialized indexes, and production storage** may be replaced with plain application code, so that the example runs locally.
- **Any extra Genie structure is marked as an addition** rather than an upstream feature.

Each `examples/lamps/<name>` package should:

- **Explain** its model mapping and the limits it chose.
- **Keep taxonomy, retrieval, agent behavior, and other business logic** outside Genie core.
- **Use only the structure facades** that fit its model.
- **Run from the repository root** with no hidden or random state.
- **Keep its tests** under `examples/lamps/<name>/tests`.

## Run and test

Every lamp is a package, and its `demo.py` is the entry point. Run it as a
module from the repository root, so that both the `examples` and `genie` packages
can be found:

```bash
python -m examples.lamps.<name>.demo
python -m pytest examples/lamps/<name>/tests -q
```

Put each lamp name in place of `<name>`:

| Lamp | Run the demo | Run the tests |
| --- | --- | --- |
| Graphiti | `python -m examples.lamps.graphiti.demo` | `python -m pytest examples/lamps/graphiti/tests -q` |
| Hindsight | `python -m examples.lamps.hindsight.demo` | `python -m pytest examples/lamps/hindsight/tests -q` |
| LangMem | `python -m examples.lamps.langmem.demo` | `python -m pytest examples/lamps/langmem/tests -q` |
| Mem0 | `python -m examples.lamps.mem0.demo` | `python -m pytest examples/lamps/mem0/tests -q` |
| MemoryOS | `python -m examples.lamps.memoryos.demo` | `python -m pytest examples/lamps/memoryos/tests -q` |

- **Each demo prints its whole run and then exits.**
- **Every run prints the same output.** No lamp reads a clock, a network, or an API key.
- **All lamps at once:** `python -m pytest examples/lamps -q`, because pytest finds every lamp test through `pytest.ini`.
- **Each demo's output is captured** in `examples/expected/lamps/<name>.out` and compared by `lib/tests/test_examples.py`. Refresh it with `python lib/scripts/capture_example_output.py` after an intended change.

## Shared agent-demo format

- **Deterministic, with no API key and no agent framework.**
- **Named tools take and return JSON-compatible values,** so an agent backed by a model could use the same boundary.
- **The application code chooses the tool calls.** Genie only supplies memory access and structures.
- **Terminal output follows one format:** title and purpose, Genie structures, tool definitions, agent answer, ordered calls, and a count per tool.
- **Each lamp guide documents only its own** stored model, workflow, results, and limits.

## Included lamps

| Lamp | Upstream idea it keeps | Typical workflow |
| --- | --- | --- |
| [Graphiti](graphiti/README.md) | Episodes, entity nodes with summaries that change over time, relationship facts on two time axes, communities | Ingest episodes, invalidate contradicted facts on both time axes, group entities by slice, and rank facts with a hybrid recipe |
| [Hindsight](hindsight/README.md) | Documents turned into world and experience facts, observations built up with proof counts, standing mental models, four-way retrieval | Retain and consolidate, refine a belief as evidence supports or reverses it, and answer from the highest layer that already knows |
| [LangMem](langmem/README.md) | Namespaced items in collection and profile shapes, memory formed during the conversation or in the background, the agent's prompt as memory | Debounce a thread, apply the changes the manager is allowed to make, and revise the prompt from feedback |
| [Mem0](mem0/README.md) | Facts pulled from an exchange and reconciled against what is on file, scoped by user, agent, and run identifiers, with a change history and a graph half | Decide `ADD`, `UPDATE`, `DELETE`, or `NONE` for each candidate, apply it, and read afterwards why a memory says what it says |
| [MemoryOS](memoryos/README.md) | An operating-system hierarchy: a short-term queue, mid-term topic sessions scored by heat, and a long-term profile beside two capped knowledge bases | Page the oldest exchange out of a full queue, let retrieval heat a session until it crosses the threshold and is read into the profile, and evict the session nobody came back to |
