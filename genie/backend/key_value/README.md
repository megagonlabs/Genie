# Redis backend

`RedisBackend` is a persistent key-value adapter built directly on the official
`redis` Python client. It implements the core backend contract and every
optional structure capability.

```bash
pip install "genie[redis]"
```

```python
from genie import Memory, RedisBackend


with RedisBackend("redis://localhost:6379/0") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

## Is Redis a key-value store or a JSON store?

Redis is a key-value store with server-side data structures: strings, hashes,
lists, sets, sorted sets, and streams. JSON is not part of core Redis. It comes
from the optional RedisJSON module, which only some deployments install.

So the accurate description is a key-value data-structure store. Calling it a
JSON store is only correct for a server that loads RedisJSON. `RedisBackend`
therefore uses core data structures only. It stores each block and each link as
one JSON document inside a plain Redis string, the same portable encoding
`SQLiteBackend` and `Neo4jBackend` use for values and metadata. That gives a
document-shaped layout on any Redis server, with no module required.

## Key layout

Every key starts with `key_prefix`, which defaults to `"genie"`. Several Genie
datasets can share one Redis database by using different prefixes.

| Key | Type | Holds |
| --- | --- | --- |
| `genie:schema` | hash | Application id and schema version |
| `genie:mutations` | string | Counter bumped by every committed Genie write |
| `genie:order:blocks`, `genie:order:links` | string | Next stable storage order |
| `genie:block:<id>` | string | One block document |
| `genie:blocks` | sorted set | Every block id in storage order |
| `genie:children:<id>` | sorted set | Direct child ids in creation order |
| `genie:link:<id>` | string | One link document |
| `genie:outlinks:<id>`, `genie:inlinks:<id>` | sorted set | Link ids by endpoint |
| `genie:relation:<relation>` | sorted set | Stored edges of one relation |
| `genie:acyclic` | set | Relations protected as acyclic graphs |
| `genie:keys:<collection>` | hash | Address-to-block bindings |
| `genie:log:<log>` | sorted set | Log entries scored by sequence |
| `genie:logcounter:<log>` | string | Next sequence for one log |
| `genie:temporal:<collection>` | hash | Temporal entries by block id |
| `genie:temporalindex:<collection>` | sorted set | Block-id order for stable paging |
| `genie:keyowner:<id>`, `genie:logmember:<id>`, `genie:temporalmember:<id>` | set | Reverse indexes used to clean up a deleted block |

## Behavior

- Values, metadata, policies, namespaces, and timestamps use strict portable
  encodings instead of Redis-specific value types.
- Writes use optimistic concurrency. The read phase records the value of
  `genie:mutations`, and the commit phase re-checks that value under `WATCH`
  before it applies every staged change in one `MULTI`/`EXEC` block. A write
  that would commit on stale reads is retried instead. This gives the same
  atomic behavior as SQLite and Neo4j: compare-and-swap updates, conditional
  subtree deletion, unique key bindings, monotonic sequences, and cycle-checked
  DAG insertion.
- Multi-command reads use the same guard value to detect a write that landed in
  the middle, and they retry when one did. A read that fits in one command
  skips that check because Redis runs each command on its own.
- Deleting a block removes the whole subtree together with its links, key
  bindings, log entries, and temporal entries in the same commit. Reverse
  indexes make that cleanup exact instead of a full scan.
- On construction the backend checks the schema record for its application id
  and version. It refuses a record owned by another application, a version
  newer than it supports, and a namespace that holds Genie keys with no record.
  `initialize_schema=False` validates the record without creating it.
- Client failures are mapped to Genie's stable `BackendError` types. The
  original error stays available as `__cause__`.
- `close()` waits for active operations before it closes the shared client.

## Concurrency and limits

Optimistic writes never hold a lock, so a stalled client cannot block others.
The cost is retries: every committed write invalidates the attempts running
beside it, so throughput drops as write contention rises. A write that cannot
commit inside `max_retry_time` raises `BackendBusy`, which is retryable. A
multi-command read that keeps being interrupted by writes reports the same
error, because it would rather retry than return a torn view.

Redis keeps the dataset in memory, and durability comes from server
configuration (RDB snapshots, AOF, or both). The adapter cannot change that, so
choose the persistence and replication settings your data needs. Genie never
flushes the database, and it only touches keys under its own prefix.

Subtree reads walk one ownership level per round trip, and portable search
scans the candidate blocks inside the scope. Use a separate search
implementation when you need large full-text or vector indexes.

The adapter targets one logical Redis database, which can be a standalone
server or a primary with replicas. Redis Cluster needs every key of a
transaction in one hash slot, so use a hash-tagged prefix such as
`key_prefix="{genie}"` there. That keeps all Genie keys in a single slot, which
also means a single node holds them.

Genie keys, documents, and indexes belong to the backend. Write through
`RedisBackend`. Writing to these keys directly can skip the reference,
temporal, and cycle checks, and it does not bump the mutation guard that keeps
concurrent Genie writes correct.

Run `../../../scripts/test_full.sh` from the repository root to start a
throwaway Redis container and run the whole suite. If you run the server
yourself, live tests need `GENIE_REDIS_URL`. The optional
`GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_USER`, and `GENIE_REDIS_PASSWORD`
variables choose the test key namespace and the authentication.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md), and
[Redis tests](../../../tests/test_redis_backend.py).
