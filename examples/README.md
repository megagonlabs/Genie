# Examples

Examples are runnable from the repository root, contain plain assertions, and
use the project Conda environment:

```bash
conda run -n "genie" python examples/<path>.py
```

## Basic reading order

1. [`basic/crud_blocks.py`](basic/crud_blocks.py): block CRUD, versions,
   ownership traversal, and recursive deletion.
2. [`basic/search_filters.py`](basic/search_filters.py): friendly search,
   portable queries/results, scopes, types, and metadata filters.
3. [`basic/scoped_agents.py`](basic/scoped_agents.py): bounded handles for
   agents and sub-agents.
4. [`basic/links.py`](basic/links.py): typed links, metadata updates, mounts,
   and linked search.
5. [`basic/policies.py`](basic/policies.py): private defaults and shared access.
6. [`basic/hooks_codecs.py`](basic/hooks_codecs.py): hooks and custom or stdlib
   codecs.
7. [`basic/stdlib_codecs.py`](basic/stdlib_codecs.py): opt-in stdlib codecs and
   raw payloads.
8. [`basic/routed_backend.py`](basic/routed_backend.py): route blocks across
   stores.
9. [`basic/sqlite_persistence.py`](basic/sqlite_persistence.py): close and
   reopen persistent blocks and links with SQLite.

[`basic/basic_usage.py`](basic/basic_usage.py) combines handles, ownership,
search, links, policies, hooks, dataclass codecs, and stdlib codecs in one agent
workflow. See the [basic guide](basic/README.md).

## Advanced structures

- [`advanced/temporal_collection.py`](advanced/temporal_collection.py): event
  time and validity periods.
- [`advanced/agent_memory.py`](advanced/agent_memory.py): an application-owned
  task memory composed from keyed preconditions, an experiment log, and a
  property graph.
- [`advanced/experiment_memory.py`](advanced/experiment_memory.py): all seven
  structures, their shared records, and atomic lineage protection in one
  workflow.

See the [advanced guide](advanced/README.md).

## Conventions

- `InMemoryBackend` makes local storage explicit and dependency-free.
- `SQLiteBackend` demonstrates that the same APIs work with persistent storage.
- Agent examples use rooted handles; unbounded handles appear only for trusted
  administrative or routing behavior.
- Application types and agent decisions stay in example code.
- Production code can use `SQLiteBackend` for local persistence or another
  adapter with suitable storage, search, and operational behavior.

Neo4j is covered in the [backend guide](../genie/backend/README.md) rather than
in a runnable example because it requires a separately configured service. The
backend protocols and operational errors are adapter-author APIs and are also
documented there instead of being presented as application workflows.
