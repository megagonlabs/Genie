# Basic Examples

These examples introduce core Genie features one at a time, followed by one
integrated workflow.

## Run

Run each script from the repository root with the project Conda environment:

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
```

## Included Examples

- `crud_blocks.py`: block CRUD and child traversal.
- `search_filters.py`: text, type, subtree, and metadata filters.
- `scoped_agents.py`: bounded handles for agents and sub-agents.
- `links.py`: typed links, mounts, and linked-memory search.
- `policies.py`: private defaults and explicitly shared access.
- `hooks_codecs.py`: hooks and custom value codecs.
- `stdlib_codecs.py`: opt-in standard-library codecs.
- `routed_backend.py`: blocks routed across backend stores.
- `basic_usage.py`: an integrated workflow using the main features.

## Conventions

- Each script uses `InMemoryBackend` for a local, dependency-free demo.
- Each script contains assertions; a successful exit confirms the expected
  behavior.
- Application-specific types and agent behavior stay in the example code.
