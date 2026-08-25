# Relational backend

`SQLiteBackend` gives you persistent local storage. It provides every core
capability and every optional one.

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The `sqlite3` driver that ships with Python is enough. You need neither the
SQLite command-line program nor an extra Python package.

## Behavior

- File databases use foreign keys, WAL, a lock timeout, and full synchronous
  durability. Setting `durability="normal"` gives up durability after a recent
  crash in exchange for speed.
- Block updates are protected by a version compare-and-swap. Recursive deletion
  checks one exact snapshot of the subtree and deletes leaves first.
- The backend checks the Genie application identity and the exact schema, and it
  runs ordered atomic migrations. An incompatible database is rejected and left
  unchanged.
- Tables hold blocks, links, keys, logs, and temporal entries. Values and
  metadata are strict JSON, and constraints and transactions keep the structure
  correct.
- Operational failures use `BackendError`. Busy and unavailable errors can be
  retried, while read-only, capacity, and corruption errors have their own
  types. The SQLite error stays available as `__cause__`.

## Limits

Portable search scans the candidates inside the scope. Use another search
implementation for large text or vector indexes. Production deployments must
decide their own backup, restore, monitoring, process, and performance
requirements.

Related: [Backend contracts](../README.md), [SQLite
tests](../../../tests/test_sqlite_backend.py), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
