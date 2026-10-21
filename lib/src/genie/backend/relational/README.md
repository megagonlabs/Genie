# Relational backends

Genie ships 2 relational adapters. Both implement all core capabilities and all
optional capabilities, and both store the same data in the same shape.

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

The `sqlite3` driver in the Python standard library is enough. The SQLite
command-line program and extra Python packages are not necessary.

### Behavior

- **Connection settings:** each connection turns on foreign keys, a lock timeout, and full synchronous durability. A file database also uses write-ahead logging (WAL). `durability="normal"` is faster, but the most recent commits can be lost after a power or operating-system crash.
- **Lock timeout:** `timeout` is the number of seconds to wait for a lock that another connection holds. Then the call raises `BackendBusy`. `timeout=0` does not wait. Construction also opens a write transaction, so the constructor waits for the lock too.
- **Block updates** use a version compare-and-swap.
- **Recursive delete** checks one exact snapshot of the subtree and deletes leaves first.
- **Construction** checks the Genie application identity and the exact schema, and runs ordered atomic migrations. It rejects an incompatible database, and the tables, the data, and the application id of that database do not change. But the constructor switches a file database to WAL mode before this check, so a rejected file stays in WAL mode.
- **Adoption:** a database with application id 0 and the exact current schema passes the check. The constructor then sets the Genie application id on it.
- **Tables** hold blocks, links, keys, logs, temporal entries, and route catalog entries. Values and metadata are strict JSON. Constraints and transactions keep the structure correct.
- **Route catalog entries** are in `route_entries`, one row for each block id, with the block id as the primary key. `RoutedBackend` uses them through `route_catalog`. A claim is one insert in a `BEGIN IMMEDIATE` transaction, and the primary key rejects a second claim for the same block id with `MemoryConflict`.
- **`route_entries` has no foreign key to `blocks`.** An entry is stored before its block, and it can name a block in another database. So block writes and deletes do not change the entries. `release_routes` removes them.
- **Operational failures** use `BackendError`. `BackendBusy` and `BackendUnavailable` are retryable. Read-only, capacity, and corruption errors have their own types. The SQLite error stays available as `__cause__`.

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

- **`user`, `password`, and `app` are placeholders.** Your own PostgreSQL user, password, and database name go in their place. Unlike the `SQLiteBackend` example above, this block does not run as written.

`psycopg` connects during the constructor. So a successful construction already
proves that the server is reachable, and there is no separate connectivity flag.

### Layout

- **One PostgreSQL schema holds all tables,** and its default name is `"genie"`. Several Genie datasets can share one database with different schema names, as `RedisBackend` does with `key_prefix`.
- **Table names match `SQLiteBackend`:** `blocks`, `links`, `acyclic_relations`, `key_bindings`, `log_counters`, `log_entries`, `temporal_entries`, and `route_entries`, plus `schema_info` for the schema record.
- **The `schema` argument has only ASCII letters, digits, and underscores.** It does not start with a digit, and it has at most 63 bytes. The reason is that the name goes into SQL as a quoted identifier, because PostgreSQL does not accept a parameter where a table name goes.

### Behavior

- **Each write takes one transaction-scoped advisory lock** before it reads or changes Genie state. PostgreSQL allows concurrent writers. Without that lock, a compound "read, check, then write" step could interleave with another writer. The lock gives the same serialized-writer model that `SQLiteBackend` gets from `BEGIN IMMEDIATE`. PostgreSQL releases it on commit or rollback.
- **Reads run in a repeatable-read, read-only transaction.** So a read with several statements sees one consistent snapshot, and it cannot fail with a serialization error.
- **Block updates use a version compare-and-swap.**
- **Recursive delete** checks one exact subtree snapshot. Then it deletes the subtree in one statement, and cascading foreign keys remove the links, key bindings, log entries, and temporal entries that belong to it.
- **Route catalog entries** in `route_entries` have the same layout as in `SQLiteBackend`, and no foreign key to `blocks`. So a recursive delete does not remove them. A claim is one insert under the advisory lock, and the primary key rejects a second claim for the same block id with `MemoryConflict`, also from another instance or process.
- **`timeout` sets `lock_timeout`.** So a write that waits too long for another writer fails with `BackendBusy` and does not hang. `timeout=0` waits with no limit, because PostgreSQL turns `lock_timeout` off at 0.
- **`durability="normal"` sets `synchronous_commit` to `off`.** This gives more write throughput, but the most recent commits can be lost after a crash. `SQLiteBackend` offers the same trade.
- **Sequence numbers come from a counter row,** not from a PostgreSQL sequence. A sequence is not transactional, so it skips a number when an append rolls back. The counter row does not skip numbers.
- **Construction** reads the schema record for its application id and version, runs ordered migrations, and then validates the exact tables, columns, indexes, and foreign keys from the system catalogs. It refuses a record of another application, a version newer than it supports, and a schema with tables but no Genie record.
- **`initialize_schema=False`** validates and runs no DDL. It suits a runtime identity that uses existing data but does not administer the schema. In this mode, no migration runs, so construction also refuses a missing schema and an older schema version.
- **Driver failures map to the stable Genie error types by SQLSTATE,** not by message text. Integrity errors (SQLSTATE class `23`) become `MemoryConflict`, and other failures become `BackendError` types. A dropped connection with no SQLSTATE becomes `BackendUnavailable`. The original error stays available as `__cause__`.

### Connection health

- **One connection, no retries.** A commit whose reply never arrives gets to the caller as `BackendUnavailable`, and the caller decides what to do.
- **So `append_log_entry` never appends the same block twice on its own,** because the adapter replays nothing. This matches `SQLiteBackend`. `Neo4jBackend`, `RedisBackend`, and `MongoDBBackend` do retry, and they have that caveat.

### Server role check

- **Construction reads `pg_is_in_recovery()`.** It refuses a connection to a read-only replica with `UnsupportedOperation`, because a replica refuses all writes.
- **It refuses only on proof.** A rejected function call, a missing row, and an unreadable value all leave the role unknown, and an unknown role passes. A connection failure still gets to the caller.
- **`verify_server_role=False`** opens a backend against a replica on purpose.
- **The role is read once.** A failover or a proxy can still move the connection to a replica later, and the backend does not detect that.

## Limits

- **Portable search scans the candidates inside the scope.** Large text or vector indexes need another search implementation. `PostgresBackend` does not use `tsvector` or a vector extension on purpose, so that its results match the reference backend.
- **JSON is stored as `text`, not `jsonb`.** `jsonb` would allow filter matching inside SQL. But it normalizes what it stores, and it rejects the NUL character inside strings. So it would not round-trip Genie values byte for byte.
- **PostgreSQL cannot store NUL in a `text` column at all.** Block ids, types, content types, principals, link relations, and key names are plain text, so PostgreSQL refuses a value with NUL. `SQLiteBackend` accepts it. JSON payloads are not affected, because the encoder writes NUL as an escape sequence, not as a raw character.
- **Each text column is declared `COLLATE "C"`.** Otherwise, PostgreSQL compares text with the database collation. That collation depends on the server locale, can ignore punctuation, and can change when the operating system upgrades its locale data. Genie needs plain byte order, because it compares stored timestamps as strings, and paging uses `block_id > ...`.
- **Production deployments decide their own** backup, restore, monitoring, process, and performance requirements.

## Tests

- **`SQLiteBackend`** tests run with a plain `pytest`.
- **`PostgresBackend`** live tests need the `postgres` extra and `GENIE_POSTGRES_DSN`.
- **`GENIE_POSTGRES_SCHEMA`** is optional and selects the schema. Its default is `genie_test`.
- **Full suite:** `lib/scripts/test_full.sh`, run from the repository root, starts disposable server containers, PostgreSQL among them, and runs the complete suite.

Related: [Backend contracts](../README.md), [document
backends](../document/README.md), [SQLite
tests](../../../../tests/backends/test_sqlite_backend.py), [PostgreSQL
tests](../../../../tests/backends/test_postgres_backend.py),
[Memory](../../memory/README.md), and [Structures](../../structures/README.md).
