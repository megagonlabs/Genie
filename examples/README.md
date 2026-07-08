# Genie Examples

These examples are meant to be read in order. Each script is runnable and uses
plain assertions, so a successful exit means the demonstrated workflow behaved
as expected.

Run examples from the repository root with the project Conda environment:

```bash
conda run -n "genie" python examples/crud_blocks.py
conda run -n "genie" python examples/scoped_agents.py
conda run -n "genie" python examples/search_filters.py
conda run -n "genie" python examples/links.py
conda run -n "genie" python examples/routed_backend.py
conda run -n "genie" python examples/policies.py
conda run -n "genie" python examples/hooks_codecs.py
conda run -n "genie" python examples/stdlib_codecs.py
conda run -n "genie" python examples/basic_usage.py
```

Recommended reading order:

1. `crud_blocks.py`: create, read, update, set, delete, and child traversal.
2. `search_filters.py`: search by text, type, subtree, and metadata filters.
3. `scoped_agents.py`: pass bounded `MemoryHandle` instances into agents and sub-agents.
4. `links.py`: create typed links, symbolic mounts, and linked-memory search.
5. `policies.py`: configure owner-private defaults and explicit shared access.
6. `hooks_codecs.py`: add hooks around memory operations with custom and stdlib codecs.
7. `stdlib_codecs.py`: opt into stdlib codecs and inspect raw payloads.
8. `routed_backend.py`: route blocks across multiple backend stores.

Integrated example:

- `basic_usage.py`: combines memory handles, parent/child ownership, search, links,
  policies, hooks, dataclass codecs, and stdlib codecs in one agent workflow.

Conventions used throughout:

- Examples pass `backend=InMemoryBackend()` explicitly so storage is visible.
- Agent examples use `memory.get_handle(root=...)` for least-privilege access.
- `memory.get_unbounded_handle(...)` appears only where the example is showing a
  trusted administrative or routing-style handle.
- `InMemoryBackend` is for examples, tests, and prototypes. Production code
  should provide a backend adapter with the desired persistence behavior.
