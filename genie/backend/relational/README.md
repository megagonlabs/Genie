# Relational backends

Genie ships two relational adapters. Both provide every core capability and
every optional one, and both store the same data in the same shape.

| Backend | Driver | Good fit |
| --- | --- | --- |
| `SQLiteBackend` | built-in `sqlite3` | A local application, and the persistent default |
| `PostgresBackend` | optional `psycopg` | An application that already runs a PostgreSQL server and needs shared access |

## `SQLiteBackend`

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The `sqlite3` driver that ships with Python is enough. You need neither the
SQLite command-line program nor an extra Python package.

### Behavior

- **File databases** use foreign keys, write-ahead logging (WAL), a lock timeout, and full synchronous durability. `durability="normal"` gives up durability after a recent crash in exchange for speed.
- **Block updates** are protected by a version compare-and-swap.
- **Recursive deletion** checks one exact snapshot of the subtree and deletes leaves first.
- **Construction** checks the Genie application identity and the exact schema, and runs ordered atomic migrations. An incompatible database is rejected and left unchanged.
- **Tables** hold blocks, links, keys, logs, and temporal entries. Values and metadata are strict JSON, and constraints and transactions keep the structure correct.
- **Operational failures** use `BackendError`. `BackendBusy` and `BackendUnavailable` can be retried, while read-only, capacity, and corruption errors have their own types. The SQLite error stays available as `__cause__`.

## `PostgresBackend`

```bash
pip install "genie[postgres]"
```

```python
from genie import Memory, PostgresBackend


with PostgresBackend("postgresql://user:password@localhost:5432/app") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

`psycopg` connects while the constructor runs, so a successful construction
already proves the server is reachable. There is no separate connectivity flag.

### Layout

- **One PostgreSQL schema holds every table,** and it defaults to `"genie"`. Several Genie datasets can share one database by using different schema names, the same way `RedisBackend` uses `key_prefix`.
- **Table names match `SQLiteBackend`:** `blocks`, `links`, `acyclic_relations`, `key_bindings`, `log_counters`, `log_entries`, and `temporal_entries`, plus `schema_info` for the schema record.
- **The `schema` argument is restricted to letters, digits, and underscores,** because it is placed into SQL as a quoted identifier. PostgreSQL does not accept a parameter where a table name belongs.

### Behavior

- **Every write takes one transaction-scoped advisory lock** before it reads or changes Genie state. PostgreSQL allows concurrent writers, so without that lock a compound "read, check, then write" step could interleave with another writer. The lock gives the same serialized-writer model that `SQLiteBackend` gets from `BEGIN IMMEDIATE`, and PostgreSQL releases it on commit or rollback.
- **Reads run in a repeatable-read, read-only transaction,** so a read made of several statements sees one consistent snapshot and cannot fail with a serialization error.
- **Block updates use a version compare-and-swap.**
- **Recursive deletion** checks one exact subtree snapshot, then deletes the subtree in one statement and lets cascading foreign keys remove the links, key bindings, log entries, and temporal entries that belong to it.
- **`timeout` sets `lock_timeout`,** so a write that waits too long for another writer fails with `BackendBusy` instead of hanging.
- **`durability="normal"` sets `synchronous_commit` to `off`,** which trades the most recent commits after a crash for write throughput — the same trade `SQLiteBackend` offers.
- **Sequence numbers come from a counter row,** not from a PostgreSQL sequence. A sequence is not transactional and would skip numbers on a rolled back append, and Genie log sequences must stay gapless.
- **Construction** reads the schema record for its application id and version, runs ordered migrations, then validates the exact tables, columns, indexes, and foreign keys from the system catalogs. It refuses a record owned by another application, a version newer than it supports, and a schema that holds tables with no Genie record. `initialize_schema=False` validates without running any DDL, for a runtime identity that may use existing data but may not administer schema.
- **Driver failures map to Genie's stable `BackendError` types by SQLSTATE** rather than by message text. The original error stays available as `__cause__`.

### Connection health

- **One connection, no retries.** A commit whose reply never arrives reaches the caller as `BackendUnavailable`, and the caller decides what to do.
- **So `append_log_entry` never appends the same block twice here,** because nothing is replayed on your behalf. This matches `SQLiteBackend`. The adapters that do retry carry that caveat instead.

### Server role check

- **Construction reads `pg_is_in_recovery()`** and refuses a connection that lands on a read-only replica, because a replica refuses every write.
- **It refuses only on proof.** A rejected function call, a missing row, and an unreadable value all leave the role unknown, and unknown passes.
- **`verify_server_role=False`** opens a backend against a replica on purpose.
- **The role is read once.** A failover or a proxy can still move the connection to a replica later, and that is not detected.

## Limits

- **Portable search scans the candidates inside the scope.** Use another search implementation for large text or vector indexes. `PostgresBackend` deliberately does not use `tsvector` or a vector extension, because its results must match the reference backend.
- **JSON is stored as `text`, not `jsonb`.** `jsonb` would allow filter matching inside SQL, but it normalizes what it stores and rejects the NUL character inside strings, so it would not round-trip Genie values byte for byte.
- **PostgreSQL cannot store NUL in a `text` column at all.** Block ids, types, content types, principals, link relations, and key names are stored as plain text, so a value containing NUL is refused. `SQLiteBackend` accepts it. JSON payloads are not affected, because the encoder writes NUL as an escape sequence rather than as a raw character.
- **Every text column is declared `COLLATE "C"`.** PostgreSQL otherwise compares text with the database collation, which depends on the server locale, can ignore punctuation, and can change when the operating system upgrades its locale data. Genie needs plain byte order, because stored timestamps are compared as strings and paging uses `block_id > ...`.
- **Production deployments decide their own** backup, restore, monitoring, process, and performance requirements.

## Tests

- **`SQLiteBackend`** tests run with a plain `pytest`.
- **`PostgresBackend`** live tests need the `postgres` extra installed and `GENIE_POSTGRES_DSN` set.
- **`GENIE_POSTGRES_SCHEMA`** is optional and chooses the schema. It defaults to `genie_test`.
- **Full suite:** run `../../../scripts/test_full.sh` from the repository root to start a throwaway PostgreSQL container and run everything.

Related: [Backend contracts](../README.md), [SQLite
tests](../../../tests/backends/test_sqlite_backend.py), [PostgreSQL
tests](../../../tests/backends/test_postgres_backend.py),
[Memory](../../memory/README.md), and [Structures](../../structures/README.md).
