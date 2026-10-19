# Lamps

A lamp is a runnable application-level memory pattern built from Genie.
Smaller single-file examples are in the [examples guide](../README.md).

- **Architectural examples, not products.** Some lamps take their ideas from other systems. They are not compatibility layers, and they are not production implementations.
- **Each lamp keeps the main memory idea** of the project that it represents, and one workflow that is typical of that project. These guides call that original project *upstream*.
- **Model calls, specialized indexes, and production storage:** a lamp can replace them with plain application code, so that the example runs locally.
- **Each lamp guide marks an extra Genie structure as an addition,** not as an upstream feature.

Each `examples/lamps/<name>` package:

- **Explains** its model mapping and the limits that it chose.
- **Keeps taxonomy, retrieval, agent behavior, and other business logic** outside Genie core.
- **Uses only the structure facades** that fit its model.
- **Runs from the repository root** with no hidden or random state.
- **Keeps its tests** under `examples/lamps/<name>/tests`.

## Run and test

Each lamp is a package, and its `demo.py` is the entry point. Run it as a
module from the repository root, so that Python finds both the `examples`
package and the `genie` package:

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

- **Each demo prints all of its run and then exits.**
- **Each run prints the same output.** No lamp reads a clock, a network, or an API key.
- **All lamps at once:** `python -m pytest examples/lamps -q`. `pytest.ini` sets the import paths for `genie` and `examples`, and its `testpaths` also include `examples/lamps`.
- **`examples/expected/lamps/<name>.out` holds the captured output of each demo.** `lib/tests/test_examples.py` compares a live run with that file. Refresh it with `python lib/scripts/capture_example_output.py` after an intended change.

## Shared agent-demo format

- **Deterministic, with no API key and no agent framework.**
- **Named tools take and return JSON-compatible values,** so an agent that uses a model can use the same boundary.
- **The application code chooses the tool calls.** Genie only supplies memory access and structures.
- **Terminal output has one format:** title and purpose, Genie structures, tool definitions, agent answer, ordered calls, and a count per tool.
- **Each lamp guide documents only its own** stored model, workflow, results, and limits.

## Included lamps

| Lamp | Upstream idea it keeps | Typical workflow |
| --- | --- | --- |
| [Graphiti](graphiti/README.md) | Episodes, entity nodes with summaries that change over time, relationship facts on 2 time axes, communities | Ingest episodes, invalidate contradicted facts on both time axes, group entities by slice, and rank facts with a hybrid recipe |
| [Hindsight](hindsight/README.md) | Documents that become world facts and experience facts, observations with proof counts that grow, standing mental models, retrieval with 4 retrievers | Retain and consolidate, refine a belief when evidence supports or reverses it, and answer from the highest layer that has the answer |
| [LangMem](langmem/README.md) | Namespaced items in collection and profile shapes, memory that forms during the conversation or in the background, the prompt of the agent as memory | Debounce a thread, apply the changes that the manager has permission to make, and revise the prompt from feedback |
| [Mem0](mem0/README.md) | Facts extracted from an exchange and reconciled with the stored facts, a scope from user, agent, and run identifiers, a change history, and a graph part for entities and their relationships | Decide `ADD`, `UPDATE`, `DELETE`, or `NONE` for each candidate, apply the decision, and later read why a memory has its current content |
| [MemoryOS](memoryos/README.md) | A hierarchy like an operating system: a short-term queue, mid-term topic sessions with a heat score, and a long-term profile next to 2 knowledge bases with a size cap | Page the oldest exchange out of a full queue, let retrieval raise the heat of a session until the heat crosses the threshold and promotion writes the session into the profile, and evict the least-used session when the tier is full |
