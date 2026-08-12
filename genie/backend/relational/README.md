# Relational backend

`SQLiteBackend` provides persistent local storage and every core and optional
backend capability.

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

Python's built-in `sqlite3` driver is sufficient; neither the SQLite CLI nor an
extra Python dependency is required.

## Behavior

- File databases use foreign keys, WAL, a lock timeout, and full synchronous
  durability. `durability="normal"` trades recent-crash durability for speed.
- Version compare-and-swap protects block updates. Recursive deletion verifies
  one exact subtree snapshot and deletes leaf-first.
- Genie application identity, exact schema validation, and ordered atomic
  migrations reject incompatible databases without modifying them.
- Tables store blocks, links, keys, logs, and temporal entries. Values and
  metadata are strict JSON; constraints and transactions enforce structure.
- Operational failures use `BackendError`; busy and unavailable errors are
  retryable, while read-only, capacity, and corruption have distinct types.
  The SQLite error remains available as `__cause__`.

## Limits

Portable search scans scoped candidates. Use another search implementation for
large text or vector indexes. Production deployments must define their backup,
restore, monitoring, process, and performance requirements.

Related: [Backend contracts](../README.md), [SQLite
tests](../../../tests/test_sqlite_backend.py), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
