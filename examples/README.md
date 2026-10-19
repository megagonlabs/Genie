# Examples

All examples run from the repository root.

- **Single-file examples** in `basic/` and `advanced/` run as scripts and use
  plain assertions: `python examples/<path>.py`.
- **Lamps** in `lamps/` are packages and run as modules:
  `python -m examples.lamps.<name>.demo`.
- **`expected/` holds the output of each example.** Read a file there to see
  the output. You do not need to run the example.

## Basic reading order

1. **[`basic/crud_blocks.py`](basic/crud_blocks.py)** — block CRUD (create, read, update, delete), versions, ownership traversal, and recursive delete.
2. **[`basic/block_presence.py`](basic/block_presence.py)** — subtree membership tests, and when a missing block is an answer and not an error.
3. **[`basic/search_filters.py`](basic/search_filters.py)** — search with friendly arguments, portable queries and results, scopes, types, and metadata filters.
4. **[`basic/scoped_agents.py`](basic/scoped_agents.py)** — bounded handles for agents and sub-agents.
5. **[`basic/links.py`](basic/links.py)** — typed links, metadata updates, mounts, and linked search.
6. **[`basic/acyclic_links.py`](basic/acyclic_links.py)** — keep one relation free of cycles with atomic inserts that check for cycles.
7. **[`basic/mounted_handles.py`](basic/mounted_handles.py)** — `allow_mounts` as a handle capability that does not widen the subtree.
8. **[`basic/policies.py`](basic/policies.py)** — the `MemoryPolicy.owner_private()` default (only the creator of a block has access), and how to share access.
9. **[`basic/hook_audit.py`](basic/hook_audit.py)** — the hook audit surface: scopes, traversal and terminal events, refusals, mandatory hooks, suppression windows, and `fail_open`.
10. **[`basic/write_hooks.py`](basic/write_hooks.py)** — change and observe each write, and refuse a write from a hook.
11. **[`basic/search_hooks.py`](basic/search_hooks.py)** — rewrite a query and review its results with hooks that transform them.
12. **[`basic/custom_codecs.py`](basic/custom_codecs.py)** — write your own codec and examine the payload that it stores.
13. **[`basic/stdlib_codecs.py`](basic/stdlib_codecs.py)** — opt-in standard-library codecs and raw payloads.
14. **[`basic/routed_backend.py`](basic/routed_backend.py)** — route blocks to different stores.
15. **[`basic/sqlite_persistence.py`](basic/sqlite_persistence.py)** — write persistent blocks and links to SQLite, close the backend, and read them through a new backend.
16. **[`basic/heterogeneous_backend.py`](basic/heterogeneous_backend.py)** — build one backend from SQL blocks, a graph topology index, and a vector search index.

**[`basic/basic_usage.py`](basic/basic_usage.py)** combines handles, ownership,
search, links, policies, hooks, dataclass codecs, and standard-library codecs
in one agent workflow. See the [basic guide](basic/README.md).

## Advanced structures

- **[`advanced/temporal_collection.py`](advanced/temporal_collection.py)** — event time, validity periods, and combined temporal filters.
- **[`advanced/graph_traversal.py`](advanced/graph_traversal.py)** — multi-hop walks in both directions with a depth limit, and edge annotation.
- **[`advanced/versioned_catalog.py`](advanced/versioned_catalog.py)** — list registered records by namespace, and prune the subtree of one record to retire it.
- **[`advanced/agent_memory.py`](advanced/agent_memory.py)** — a task memory that the application owns, made from keyed preconditions, an experiment log, and a property graph.
- **[`advanced/experiment_memory.py`](advanced/experiment_memory.py)** — all 7 structures, their shared records, and atomic lineage protection in one workflow.
- **[`advanced/structure_hooks.py`](advanced/structure_hooks.py)** — structural hook events, so an audit trail records how the application organized memory, and also what memory stored.
- **[`advanced/facade_containers.py`](advanced/facade_containers.py)** — the ordinary block that each facade binds to, and the result after a delete of that block.

See the [advanced guide](advanced/README.md).

## Lamps

**[`lamps/`](lamps/README.md)** holds larger, application-level memory patterns
built from Genie. Each lamp is a package with its own guide, demo, and tests.

| Lamp | Run the demo | Run the tests |
| --- | --- | --- |
| [Graphiti](lamps/graphiti/README.md) | `python -m examples.lamps.graphiti.demo` | `python -m pytest examples/lamps/graphiti/tests -q` |
| [Hindsight](lamps/hindsight/README.md) | `python -m examples.lamps.hindsight.demo` | `python -m pytest examples/lamps/hindsight/tests -q` |
| [LangMem](lamps/langmem/README.md) | `python -m examples.lamps.langmem.demo` | `python -m pytest examples/lamps/langmem/tests -q` |
| [Mem0](lamps/mem0/README.md) | `python -m examples.lamps.mem0.demo` | `python -m pytest examples/lamps/mem0/tests -q` |
| [MemoryOS](lamps/memoryos/README.md) | `python -m examples.lamps.memoryos.demo` | `python -m pytest examples/lamps/memoryos/tests -q` |

- **Run them as modules,** not as scripts, because each lamp is a package.
- **Architectural demonstrations, not products.** See the [lamps guide](lamps/README.md).

## Captured output

Each example has a captured-output file under [`expected/`](expected), with the
name of the example. `lib/tests/test_examples.py` runs each example and compares
all of its standard output with that file.

- **Refresh the files after an intended output change:**
  `python lib/scripts/capture_example_output.py`. The script runs each example
  again, writes the files again, and deletes captured output that no example
  owns.
- **Each example is registered** in `RUNNABLE_EXAMPLES` in
  `lib/tests/test_examples.py`. `test_every_example_script_has_smoke_coverage`
  fails if a new script or lamp demo is not in that list.
- **Output is deterministic.** No example reads a clock, a network, or a random
  source, and no example prints a block id. `test_example_matches_its_captured_output`
  fails if a run prints output that is different from the captured file.

## Conventions

- **`InMemoryBackend`** makes local storage explicit and needs no extra packages.
- **`SQLiteBackend`** shows that the same APIs work with persistent storage.
- **Agent examples use rooted handles.** Unbounded handles occur only in trusted administrative or routing code.
- **Application types and agent decisions** stay in the example code.
- **For production,** use `SQLiteBackend` for local persistence, or another adapter with suitable storage, search, and operational behavior.
- **Server-backed adapters have no runnable example,** because each one needs a service that you configure separately. The [backend guide](../lib/src/genie/backend/README.md) covers them.
- **Backend protocols and operational errors** are APIs for adapter authors. That guide documents them too. No example shows them as application workflows.
