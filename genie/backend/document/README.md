# MongoDB backend

`MongoDBBackend` is a persistent document adapter built directly on the official
`pymongo` driver. It implements the core backend contract and every optional
structure capability.

```bash
pip install "genie[mongodb]"
```

```python
from genie import Memory, MongoDBBackend


with MongoDBBackend("mongodb://localhost:27017/?replicaSet=rs0") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The constructor talks to the server, so a successful construction already
proves the deployment is reachable. There is no separate connectivity flag.

## Deployment target

**Use a replica set or a sharded cluster. A standalone `mongod` is not
supported.**

- **Every Genie write is one multi-document transaction,** and MongoDB runs those only on a replica set or a sharded cluster. A standalone server refuses the first write.
- **A single-member replica set is enough** for a local application, and it is one flag on the server: `mongod --replSet rs0`, then `rs.initiate()` once.
- **Construction refuses a standalone server,** and only on proof: the reply must name no replica set, must not come from a `mongos`, and must report a writable server. A blocked command, a missing field, and an unreadable value all pass.
- **`verify_transaction_support=False`** skips that check and accepts failing writes.
- **MongoDB 4.4 or newer** is the supported target. Transactions need 4.0, and 4.4 is where the rest of what the adapter relies on settled: `hello`, unlimited index keys, and transactions on a sharded cluster.

## Collection layout

One MongoDB database holds every Genie collection, and it defaults to
`"genie"`. Several Genie datasets can share one server by using different
database names, the same way `PostgresBackend` uses a schema.

| Collection | Holds | `_id` |
| --- | --- | --- |
| `schema_info` | Application id and schema version | `"genie"` |
| `guard` | Lock counter every write increments | `"genie.mutations"` |
| `counters` | Next stable storage order | `"blocks"`, `"links"` |
| `blocks` | One block document | Block id |
| `links` | One typed link document | Link id |
| `acyclic_relations` | Relations protected as acyclic graphs | Relation |
| `key_bindings` | Address-to-block bindings | Collection, namespace, and key |
| `log_counters` | Next sequence for one log | Log id |
| `log_entries` | One ordered log entry | Log id and sequence |
| `temporal_entries` | One temporal collection entry | Collection id and block id |

- **Collection names match the table names in `SQLiteBackend` and `PostgresBackend`,** and every field carries the same portable encoding those two and `Neo4jBackend` use, so one record is easy to recognize from either side. `RedisBackend` differs: it keeps one JSON document per block with a nested value.
- **A composite `_id` is a JSON array of its parts,** which cannot be produced by two different field pairs. Genie names every index it creates, so index names stay the same on every server version.
- **The `database` argument is checked before the driver connects** against MongoDB's own rules: at most 63 bytes, and none of `` / \ . " $ * < > : | ? `` or a space.

## Encodings

- **Portable encodings** are used for values, metadata, policies, namespaces, and timestamps, instead of BSON-specific value types.
- **Values and metadata are stored as JSON text, not as BSON subdocuments.** This is the same choice `PostgresBackend` makes with `text` instead of `jsonb`, for the same reason: BSON cannot round-trip every JSON object. A key containing a dot, a key starting with `$`, and an integer larger than 64 bits all change or fail on the way in.
- **Caller data never becomes a field name.** Block ids, key names, and relations are stored as values, and every field name in a stored document is a Genie constant. So a `$`-prefixed or dotted key inside a value is just text.
- **Timestamps are ISO 8601 UTC strings.** A BSON date holds milliseconds and would quietly drop the microseconds a caller supplied.

## Writes

- **Every write runs inside one transaction** that first increments the shared guard document. Two writers cannot hold that document at once, so a compound "read, check, then write" step is atomic against every other Genie writer. This is the serialized-writer model `SQLiteBackend` gets from `BEGIN IMMEDIATE` and `PostgresBackend` gets from an advisory lock.
- **The loser retries instead of blocking.** MongoDB gives a transaction no lock to wait on, so the writer that loses the guard is aborted with a write conflict and started again. `max_retry_time` bounds how long that can go on before `BackendBusy` is raised.
- **Same atomic behavior as `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `RedisBackend`:** compare-and-swap updates, conditional subtree deletion, unique key bindings, monotonic sequences, and cycle-checked DAG insertion.
- **Deleting a block removes the whole subtree** together with its links, key bindings, log entries, and temporal entries in the same transaction. MongoDB has no cascading reference, so the adapter deletes those records itself.
- **Sequence numbers come from a counter document.** An aborted transaction rolls the counter back with everything else it wrote, so log sequences stay gapless.
- **Long id lists are split into chunks of 500** before they go into a `$in` filter, because one command document may not exceed 16 MB.

## Reads

| Shape | How it runs | Extra round trip? |
| --- | --- | --- |
| Fits in one command | Nothing extra, because MongoDB applies one command atomically | No |
| Needs several commands | One transaction, so every command reads the same snapshot | Yes, for the commit |

- **Single-command reads:** `get_block`, `children`, `list_links`, `get_key`, `list_keys`, `get_log_entry`, `list_log_entries`, `get_temporal_entry`, `list_temporal_entries`, and `query_temporal_entries`.
- **Snapshot reads:** `ancestors`, `descendants`, `contains`, and `search`. Each walks the ownership tree or joins links, so it must not see half of a write.
- **`contains` walks up from the candidate,** which reads one document per level instead of the whole subtree.
- **`descendants` reads one ownership level per round trip,** then walks the result depth-first with each level of children in storage order. That gives the same order as the recursive query in `SQLiteBackend` and `PostgresBackend`.
- **All reads target the primary,** so a write is always visible to the read that follows it.

## Durability

| `durability` | Write concern | Read concern | Trade |
| --- | --- | --- | --- |
| `"full"` (default) | `majority` with journal | `majority`, `snapshot` inside a transaction | Nothing a read returns can be rolled back |
| `"normal"` | Primary only (`w=1`) | `local` | Faster, but a failover can roll back the most recent commits |

- **`"normal"` reads with `local` on purpose.** A `w=1` write is not majority-committed yet, so a majority read would not see the write that just returned.
- **Server configuration still decides the rest:** replica set size, oplog size, backups, and monitoring.

## Retried commits

**A commit whose reply never arrives is retried, and the retry can repeat
work.**

- **Why:** when the connection dies during the commit, nobody knows whether the server applied it. MongoDB labels that `UnknownTransactionCommitResult`, and the adapter retries the commit. If the retry reports a transient failure, the whole transaction runs again.
- **Most repeats raise `MemoryConflict`,** because the write checks for its own result first: a block id, a link id, a key address, or a temporal entry that already exists, and any `expected_version` or `expected_block_id` write whose expected value has moved. The write did land, but the caller is told it conflicted.
- **Some repeats end at the same state:** `update_block` without a version, `rebind_key` to the same target, and every cleanup that follows a deleted block.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key, so a repeat appends the same block a second time at a new sequence, and the caller receives the second entry. `Neo4jBackend` and `RedisBackend` carry the same limit for the same reason. An application that needs exactly-once appends should carry its own token in the block and skip a duplicate on read.
- **Past `max_retry_time`** an unknown commit result reaches the caller as `BackendUnavailable`, and a repeated conflict reaches it as `BackendBusy`. Both are retryable.

## Schema and errors

- **Construction reads the schema record** for its application id and version, runs ordered migrations, then validates the exact collections, indexes, and control documents. It refuses a record owned by another application, a version newer than it supports, and a database that holds collections with no Genie record. Every migration step is idempotent, so an interrupted initialization can be resumed.
- **`initialize_schema=False` validates without creating anything,** for a runtime identity that may use existing data but may not administer collections.
- **A locale collation is refused.** MongoDB compares strings byte by byte unless a collection declares one, and Genie compares stored timestamps as strings and pages with `block_id > ...`. This is the same requirement `PostgresBackend` meets with `COLLATE "C"`.
- **Driver failures map to Genie's stable `BackendError` types by server error code** rather than by message text. A transport failure carries no code, so it is recognized by its driver exception type. The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared client.

**Permissions needed:**

- **The schema initializer** needs permission to create collections and indexes in the Genie database.
- **Runtime identities** need read and write access to the Genie collections, plus permission to list collections and indexes.

## Server role check

**Point the URI at the deployment, not at one node.**

- **Construction refuses a connection that lands on a node which refuses writes,** such as a secondary reached with `directConnection=true`.
- **Writes on a secondary already fail loudly** with an error that reaches the caller as `BackendReadOnly`. The silent case is reads: with `initialize_schema=False` a backend on a secondary opens, never writes, and serves data that can lag the primary with no signal.
- **It refuses only on proof.** The check reads one `hello` reply, and falls back to `ismaster` on a server that predates it. A blocked command, a missing field, and an unreadable value all pass.
- **`verify_server_role=False`** opens a backend against such a node on purpose.
- **The role is read once.** A failover or a proxy can still move this connection later, and that is not detected.

## Limits

- **Portable search scans the candidates inside the scope,** then matches every candidate in Python. `MongoDBBackend` deliberately does not use a text index or Atlas Search, because its results must match the reference backend. Use a separate search implementation when you need large full-text or vector indexes.
- **Filters cannot run in the database.** Values and metadata are stored as JSON text, so a query on a nested field is not something MongoDB can index here.
- **A transaction has server-side limits.** The default lifetime is 60 seconds (`transactionLifetimeLimitSeconds`), and the changes one transaction makes must fit a 16 MB oplog entry. Deleting a very large subtree in one call can reach either limit, so keep ownership subtrees to a size you are willing to delete at once.
- **A sort outside an index has a 100 MB memory limit.** Listing and paging use indexed sorts, but a temporal query that combines several filters can fall back to an in-memory sort on a very large collection.
- **Write throughput is bounded by the guard document,** because every Genie write increments it. `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` make the same trade, and here the cost shows up as retries rather than as waiting.
- **Write through `MongoDBBackend`.** Genie collections, documents, and indexes belong to the backend. Writing to them directly can skip the reference, temporal, and cycle checks, and it does not touch the guard that keeps concurrent Genie writes correct.
- **Production deployments decide their own** backup, restore, monitoring, process, and performance requirements.

## Tests

- **Full suite:** run `../../../scripts/test_full.sh` from the repository root to start a throwaway single-member MongoDB replica set and run everything.
- **Your own server:** live tests need `GENIE_MONGODB_URI`.
- **Optional variables:** `GENIE_MONGODB_DATABASE` chooses the test database. It defaults to `genie_test`.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md),
[key-value backends](../key_value/README.md), and [MongoDB
tests](../../../tests/backends/test_mongodb_backend.py).
