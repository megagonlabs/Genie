# Genie Examples

Run examples from the repository root with the project Conda environment:

```bash
conda run -n "genie" python examples/crud_blocks.py
conda run -n "genie" python examples/scoped_agents.py
conda run -n "genie" python examples/search_filters.py
conda run -n "genie" python examples/links.py
conda run -n "genie" python examples/policies.py
conda run -n "genie" python examples/hooks_codecs.py
conda run -n "genie" python examples/basic_usage.py
```

Small examples:

- `crud_blocks.py`: create, read, patch, set, delete, and child traversal.
- `scoped_agents.py`: pass `MemoryScope` handles into agents and sub-agents.
- `search_filters.py`: search by text, type, subtree, and metadata filters.
- `links.py`: create typed links and search linked memory.
- `policies.py`: enforce principal and role based access.
- `hooks_codecs.py`: add lifecycle hooks and a custom value codec.

Integrated example:

- `basic_usage.py`: combines scopes, hierarchy, search, links, policies, hooks,
  and codecs in one agent workflow.
