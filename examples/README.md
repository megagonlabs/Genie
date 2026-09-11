# Examples

Everything here runs from the repository root.

- **Single-file examples** in `basic/` and `advanced/` run as scripts and use
  plain assertions: `python examples/<path>.py`.
- **Lamps** in `lamps/` are packages and run as modules:
  `python -m examples.lamps.<name>.demo`.
- **`expected/` holds what each example prints.** Read it to see the output
  without running anything.

## Basic reading order

1. **[`basic/crud_blocks.py`](basic/crud_blocks.py)** — block CRUD, versions, ownership traversal, and recursive deletion.
2. **[`basic/block_presence.py`](basic/block_presence.py)** — subtree membership tests, and which missing blocks are an answer rather than an error.
3. **[`basic/search_filters.py`](basic/search_filters.py)** — friendly search, portable queries and results, scopes, types, and metadata filters.
4. **[`basic/scoped_agents.py`](basic/scoped_agents.py)** — bounded handles for agents and sub-agents.
5. **[`basic/links.py`](basic/links.py)** — typed links, metadata updates, mounts, and linked search.
6. **[`basic/acyclic_links.py`](basic/acyclic_links.py)** — keep one relation free of cycles with atomic, cycle-checked inserts.
7. **[`basic/mounted_handles.py`](basic/mounted_handles.py)** — `allow_mounts` as a handle capability that does not widen the subtree.
8. **[`basic/policies.py`](basic/policies.py)** — the `MemoryPolicy.owner_private()` default, and sharing access.
9. **[`basic/hook_audit.py`](basic/hook_audit.py)** — the hook audit surface, from scopes and traversal events to refusals and suppression windows.
10. **[`basic/write_hooks.py`](basic/write_hooks.py)** — change and observe every write, and refuse one from a hook.
11. **[`basic/search_hooks.py`](basic/search_hooks.py)** — rewrite a query and review its results with transforming hooks.
12. **[`basic/custom_codecs.py`](basic/custom_codecs.py)** — write your own codec and look at the payload it stores.
13. **[`basic/stdlib_codecs.py`](basic/stdlib_codecs.py)** — opt-in standard-library codecs and raw payloads.
14. **[`basic/routed_backend.py`](basic/routed_backend.py)** — route blocks across stores.
15. **[`basic/sqlite_persistence.py`](basic/sqlite_persistence.py)** — close and reopen persistent blocks and links with SQLite.
16. **[`basic/heterogeneous_backend.py`](basic/heterogeneous_backend.py)** — build one backend from SQL blocks, a graph topology index, and a vector search index.

**[`basic/basic_usage.py`](basic/basic_usage.py)** puts handles, ownership,
search, links, policies, hooks, dataclass codecs, and standard-library codecs
together in one agent workflow. See the [basic guide](basic/README.md).

## Advanced structures

- **[`advanced/temporal_collection.py`](advanced/temporal_collection.py)** — event time, validity periods, and combined temporal filters.
- **[`advanced/graph_traversal.py`](advanced/graph_traversal.py)** — multi-hop walks in both directions with a depth limit, and edge annotation.
- **[`advanced/versioned_catalog.py`](advanced/versioned_catalog.py)** — list registered records by namespace, and retire one by pruning its subtree.
- **[`advanced/agent_memory.py`](advanced/agent_memory.py)** — a task memory owned by the application, built from keyed preconditions, an experiment log, and a property graph.
- **[`advanced/experiment_memory.py`](advanced/experiment_memory.py)** — all seven structures, their shared records, and atomic lineage protection in one workflow.
- **[`advanced/structure_hooks.py`](advanced/structure_hooks.py)** — structural hook events, so an audit trail records how memory was organized as well as what it stored.
- **[`advanced/facade_containers.py`](advanced/facade_containers.py)** — the ordinary block that each facade is bound to, and what happens when it is deleted.

See the [advanced guide](advanced/README.md).

## Lamps

**[`lamps/`](lamps/README.md)** holds larger, application-level memory patterns
built from Genie. Each one is a package with its own guide, demo, and tests.

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

Every example has a captured-output file under
[`expected/`](expected), named after the example. `lib/tests/test_examples.py`
runs each example and compares its whole standard output against that file.

- **Refresh them after an intended output change:**
  `python lib/scripts/capture_example_output.py`. It reruns every example,
  rewrites the files, and deletes captured output that no example owns.
- **Every example must be registered** in `RUNNABLE_EXAMPLES` in
  `lib/tests/test_examples.py`. `test_every_example_script_has_smoke_coverage`
  fails when a new script or lamp demo is missing from that list.
- **Output must be deterministic.** No example reads a clock, a network, a
  random source, or a block id it prints.

## Conventions

- **`InMemoryBackend`** makes local storage explicit and needs no extra packages.
- **`SQLiteBackend`** shows that the same APIs work with persistent storage.
- **Agent examples use rooted handles.** Unbounded handles appear only for trusted administrative or routing behavior.
- **Application types and agent decisions** stay in the example code.
- **For production,** use `SQLiteBackend` for local persistence, or another adapter with suitable storage, search, and operational behavior.
- **Server-backed adapters have no runnable example,** because they need a separately configured service. They are covered in the [backend guide](../lib/src/genie/backend/README.md).
- **Backend protocols and operational errors** are APIs for adapter authors, so they are documented in that guide too rather than shown as application workflows.
