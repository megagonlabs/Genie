# Genie Examples

These examples are meant to be read in order. Each script is runnable and uses
plain assertions, so a successful exit means the demonstrated workflow behaved
as expected.

Run examples from the repository root with the project Conda environment:

```bash
conda run -n "genie" python examples/basic/crud_blocks.py
conda run -n "genie" python examples/basic/scoped_agents.py
conda run -n "genie" python examples/basic/search_filters.py
conda run -n "genie" python examples/basic/links.py
conda run -n "genie" python examples/basic/routed_backend.py
conda run -n "genie" python examples/basic/policies.py
conda run -n "genie" python examples/basic/hooks_codecs.py
conda run -n "genie" python examples/basic/stdlib_codecs.py
conda run -n "genie" python examples/basic/basic_usage.py
conda run -n "genie" python examples/advanced/temporal_collection.py
conda run -n "genie" python examples/advanced/agent_memory.py
conda run -n "genie" python examples/advanced/experiment_memory.py
```

Basic examples live in [`examples/basic`](basic). Recommended reading order:

1. `basic/crud_blocks.py`: create, read, update, set, delete, and child traversal.
2. `basic/search_filters.py`: search by text, type, subtree, and metadata filters.
3. `basic/scoped_agents.py`: pass bounded `MemoryHandle` instances into agents and sub-agents.
4. `basic/links.py`: create typed links, symbolic mounts, and linked-memory search.
5. `basic/policies.py`: configure owner-private defaults and explicit shared access.
6. `basic/hooks_codecs.py`: add hooks around memory operations with custom and stdlib codecs.
7. `basic/stdlib_codecs.py`: opt into stdlib codecs and inspect raw payloads.
8. `basic/routed_backend.py`: route blocks across multiple backend stores.

Integrated example:

- `basic/basic_usage.py`: combines memory handles, parent/child ownership, search, links,
  policies, hooks, dataclass codecs, and stdlib codecs in one agent workflow.

Advanced structure examples live in [`examples/advanced`](advanced):

- `advanced/temporal_collection.py`: use event time and validity periods in
  one temporal collection.
- `advanced/agent_memory.py`: give an agent one compound memory made from
  keyed preconditions, an experiment log, and a property graph.
- `advanced/experiment_memory.py`: use all seven optional structures in one
  experiment-memory workflow.

Conventions used throughout:

- Examples pass `backend=InMemoryBackend()` explicitly so storage is visible.
- Agent examples use `memory.get_handle(root=...)` for least-privilege access.
- `memory.get_unbounded_handle(...)` appears only where the example is showing a
  trusted administrative or routing-style handle.
- `InMemoryBackend` is for examples, tests, and prototypes. Production code
  should provide a backend adapter with the desired persistence behavior.
