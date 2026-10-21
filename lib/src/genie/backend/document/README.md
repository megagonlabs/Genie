# MongoDB backend

`MongoDBBackend` is a persistent document adapter. It uses the official
`pymongo` driver directly. It implements the core backend contract, all
optional structure capabilities, and `RouteCatalog`.

```bash
pip install "genie[mongodb]"
```

```python
from genie import Memory, MongoDBBackend


with MongoDBBackend("mongodb://localhost:27017/?replicaSet=rs0") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The constructor talks to the server. So a successful construction already
proves that the deployment is reachable, and there is no separate connectivity
flag.

## Deployment target

**The supported targets are a replica set and a sharded cluster. A standalone
`mongod` is not supported.**

- **Each Genie write is one multi-document transaction,** and MongoDB runs those only on a replica set or a sharded cluster. A standalone server refuses the first write.
- **A single-member replica set is enough** for a local application. It needs one server flag: `mongod --replSet rs0`, then `rs.initiate()` one time.
- **Construction refuses a standalone server, and only on proof.** The reply names no replica set, does not come from a `mongos`, and reports a writable server. A blocked command, a missing field, and an unreadable value all pass.
- **`verify_transaction_support=False`** skips that check and accepts failing writes.
- **MongoDB 4.4 or newer** is the version that the adapter is written for. Transactions need 4.0. In 4.4, the other features that the adapter uses are stable: `hello`, unlimited index keys, and transactions on a sharded cluster.
- **Only `mongo:7` is tested.** `lib/scripts/test_full.sh` starts one image, which `GENIE_TEST_MONGODB_IMAGE` selects. A server between 4.4 and 7 meets the requirements above, but no test run covers it. So a user of such a version checks it before they depend on it.

## Collection layout

One MongoDB database holds all Genie collections. Its default name is
`"genie"`. Several Genie datasets can share one server with different database
names, as `PostgresBackend` does with a schema.

| Collection | Holds | `_id` |
| --- | --- | --- |
| `schema_info` | Application id and schema version | `"genie"` |
| `guard` | Lock counter that each write increments | `"genie.mutations"` |
| `counters` | Next stable storage order | `"blocks"`, `"links"` |
| `blocks` | One block document | Block id |
| `links` | One typed link document | Link id |
| `acyclic_relations` | Relations protected as acyclic graphs | Relation |
| `key_bindings` | Address-to-block bindings | Collection, namespace, and key |
| `log_counters` | Next sequence for one log | Log id |
| `log_entries` | One ordered log entry | Log id and sequence |
| `temporal_entries` | One temporal collection entry | Collection id and block id |
| `route_entries` | Owning route of one block id, for `RoutedBackend` | Block id |

- **The record collections have the same names as the tables in `SQLiteBackend` and `PostgresBackend`.** `schema_info` matches only `PostgresBackend`, and `guard` and `counters` have no table in either. Each field uses the same portable encoding as those two and `Neo4jBackend`, so one record is easy to recognize from each side. `RedisBackend` is different: it keeps one JSON document for each block, with a nested value.
- **`route_entries` has no reference to `blocks`.** An entry can name a block id that this database does not hold, and a block write or delete does not change an entry. So one database can hold the route catalog of a `RoutedBackend` and also be one of its routes.
- **A composite `_id` is a JSON array of its parts.** So 2 different sets of fields cannot give the same `_id`. Genie names each index that it creates, so index names stay the same on all server versions.
- **The constructor checks the `database` argument before the driver connects,** against the rules of MongoDB: at most 63 bytes, and none of `` / \ . " $ * < > : | ? ``, a space, or a NUL character.

## Encodings

- **Portable encodings** for values, metadata, policies, namespaces, and timestamps, not BSON-specific value types.
- **Values and metadata are JSON text, not BSON subdocuments.** `PostgresBackend` makes the same choice with `text` instead of `jsonb`, for the same reason: BSON cannot round-trip all JSON objects. A key with a dot, a key that starts with `$`, and an integer larger than 64 bits all change or fail on the way in.
- **Caller data never becomes a field name.** Block ids, key names, and relations are values, and each field name in a stored document is a Genie constant. So a `$`-prefixed or dotted key inside a value is only text.
- **A non-string id never becomes a filter.** Each method that takes a block, link, log, or collection id, or a relation, raises `TypeError` for a non-string. MongoDB reads a dictionary such as `{"$ne": None}` as a query operator, and it would match documents that the caller did not name.
- **Timestamps are ISO 8601 UTC strings.** A BSON date holds milliseconds, and it would silently drop the microseconds that a caller gave.

## Writes

- **Each write runs inside one transaction,** and the transaction first increments the shared guard document. 2 writers cannot hold that document at the same time. So a compound "read, check, then write" step is atomic against all other Genie writers. This is the serialized-writer model that `SQLiteBackend` gets from `BEGIN IMMEDIATE` and `PostgresBackend` gets from an advisory lock.
- **The loser retries and does not block.** MongoDB gives a transaction no lock to wait on. So the writer that loses the guard is aborted with a write conflict and starts again. `max_retry_time` limits how long that can continue before the backend raises `BackendBusy`. With `max_retry_time=0`, the first conflict raises `BackendBusy`.
- **Same atomic behavior as `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `RedisBackend`:** compare-and-swap updates, conditional subtree delete, unique key bindings, monotonic sequences, and DAG inserts with a cycle check.
- **`claim_route` stores an entry only when its block id has none,** in one write transaction. So of concurrent claims for one block id, from one instance or from several, one succeeds and each other claim raises `MemoryConflict`.
- **A delete of a block removes the whole subtree,** with its links, key bindings, log entries, and temporal entries, in the same transaction. MongoDB has no cascading reference, so the adapter deletes those records itself.
- **Sequence numbers come from a counter document.** An aborted transaction rolls the counter back with all other changes that it made. So an aborted append leaves no gap. A delete of a member block still removes its entry, and the log does not use that number again.
- **Long id lists are split into chunks of 500** before they go into a `$in` filter, because one command document cannot be larger than 16 MB.

## Reads

| Shape | How it runs | Extra round trip? |
| --- | --- | --- |
| Fits in one command | Nothing extra, because MongoDB applies one command atomically | No |
| Needs several commands | One transaction, so each command reads the same snapshot | Yes, for the commit |

- **Single-command reads:** `get_block`, `children`, `list_links`, `get_key`, `list_keys`, `get_log_entry`, `list_log_entries`, `get_temporal_entry`, `list_temporal_entries`, `query_temporal_entries`, and `get_route`.
- **Snapshot reads:** `ancestors`, `descendants`, `contains`, and `search`. Each one walks the ownership tree or joins links, so it cannot see half of a write.
- **`contains` walks up from the candidate.** So it reads one document for each level, not the whole subtree.
- **`descendants` reads one ownership level at a time,** in chunks of 500 ids. Then it walks the result depth-first, with each level of children in storage order. This gives the same order as the recursive query in `SQLiteBackend` and `PostgresBackend`.
- **All reads go to the primary.** So the read after a write always sees that write.

## Durability

| `durability` | Write concern | Read concern | Trade |
| --- | --- | --- | --- |
| `"full"` (default) | `majority` with journal | `majority`, `snapshot` inside a transaction | A failover cannot roll back anything that a read returns |
| `"normal"` | Primary only (`w=1`) | `local` | Faster, but a failover can roll back the most recent commits |

- **`"normal"` reads with `local` on purpose.** A `w=1` write is not majority-committed yet. So a majority read does not see the write that just returned.
- **Both modes run the full capability contracts.** So the tests cover `"normal"` end to end, not only its concern settings.
- **The server configuration still decides the rest:** replica set size, oplog size, backups, and monitoring.

## Retried commits

**If the reply to a commit never arrives, the adapter retries the commit, and the
retry can repeat work.**

- **Why:** when the connection dies during the commit, nobody knows whether the server applied it. MongoDB labels that `UnknownTransactionCommitResult`, and the adapter retries the commit. If the retry reports a transient failure, the whole transaction runs again.
- **Most repeats raise `MemoryConflict`,** because the write first checks for its own result: a block id, a link id, a key address, a temporal entry, or a route catalog entry that already exists, and each `expected_version`, `expected_versions`, or `expected_block_id` write whose expected value has moved. The write did land, but the caller gets a conflict.
- **A repeated `claim_route` keeps its entry.** The caller gets `MemoryConflict`, so `RoutedBackend.put_block` stores no block. The block id then has an entry and no block, and each later put of that id raises `MemoryConflict`. `release_routes` removes the entry.
- **Some repeats end at the same state:** `update_block` without a version, `rebind_key` to the same target, `release_routes`, and each cleanup that follows a deleted block.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key. So a repeat appends the same block a second time at a new sequence, and the caller gets the second entry. `Neo4jBackend` and `RedisBackend` have the same limit for the same reason. An application that needs exactly-once appends puts its own token in the block and skips a duplicate on read.
- **After `max_retry_time`,** an unknown commit result gets to the caller as `BackendUnavailable`, and a repeated conflict gets to it as `BackendBusy`. Both are retryable.

## Schema and errors

- **Construction reads the schema record** for its application id and version. It runs ordered migrations, then it checks the exact collections, indexes, and control documents. It refuses a record of another application, a version newer than it supports, and a database with collections but no Genie record. Each migration step is idempotent, so an interrupted initialization can continue later.
- **`initialize_schema=False` only checks, and it creates nothing.** It is for a runtime identity that can use existing data but cannot administer collections. It also refuses an older schema version, because it does not migrate.
- **A locale collation is refused.** MongoDB compares strings byte by byte unless a collection declares a collation. Genie compares stored timestamps as strings and pages with `block_id > ...`. `PostgresBackend` meets the same requirement with `COLLATE "C"`.
- **Driver failures map to the stable `BackendError` types of Genie,** first by server error code and by driver exception type. A transport failure has no code, so its driver exception type identifies it. For capacity and corruption, the mapping also matches the message text (`quota`, `no space left`, `corrupt`). A duplicate key becomes `MemoryConflict`. The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared client.

**Permissions needed:**

- **The schema initializer** needs permission to create collections and indexes in the Genie database.
- **Runtime identities** need read and write access to the Genie collections, and permission to list collections and indexes.

## Server role check

**The URI names the deployment, not one node.**

- **Construction refuses a connection to a node that refuses writes,** such as a secondary with `directConnection=true`.
- **Writes on a secondary already fail loudly** with an error that gets to the caller as `BackendReadOnly`. Reads are the silent case: with `initialize_schema=False`, a backend on a secondary opens, never writes, and serves data that can lag the primary with no signal.
- **It refuses only on proof.** The check reads one `hello` reply. If `hello` fails, it reads `ismaster`, for a server that does not have `hello`. A blocked command, a missing field, and an unreadable value all pass.
- **`verify_server_role=False`** opens a backend against such a node on purpose.
- **The role is read one time.** A failover or a proxy can still move this connection later, and the backend does not detect that.

## Limits

- **Portable search scans the candidates inside the scope,** then matches each candidate in Python. `MongoDBBackend` does not use a text index or Atlas Search on purpose, because its results match the reference backend. A separate search implementation suits large full-text or vector indexes.
- **Filters cannot run in the database.** Values and metadata are JSON text, so MongoDB cannot index a query on a nested field here.
- **`list_keys` reads all bindings of a collection for each page,** then filters, sorts, and cuts them in Python. A very large keyed collection makes each page slow.
- **A transaction has server-side limits.** The default lifetime is 60 seconds (`transactionLifetimeLimitSeconds`), and the changes of one transaction fit in one 16 MB oplog entry. A delete of a very large subtree in one call can reach either limit. So an ownership subtree stays at a size that one call can delete.
- **A sort outside an index has a 100 MB memory limit.** Listing and paging use indexed sorts. But on a very large collection, a temporal query with several filters can fall back to an in-memory sort.
- **The guard document limits write throughput,** because each Genie write increments it. `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` make the same trade. Here the cost shows as retries, not as waits.
- **Only `MongoDBBackend` writes the Genie collections safely.** Genie collections, documents, and indexes belong to the backend. A direct write can skip the reference, temporal, and cycle checks, and it does not touch the guard that keeps concurrent Genie writes correct.
- **Production deployments decide their own** backup, restore, monitoring, process, and performance requirements.

## Tests

- **Full suite:** `lib/scripts/test_full.sh`, run from the repository root, starts disposable server containers, a single-member MongoDB replica set among them, and runs the complete suite.
- **Your own server:** live tests need `GENIE_MONGODB_URI`.
- **Optional variables:** `GENIE_MONGODB_DATABASE` selects the test database, and its default is `genie_test`. `GENIE_TEST_MONGODB_IMAGE` selects the server image, and its default is `mongo:7`.
- **What one member cannot reach.** The disposable replica set has one member. So no live test drives a failover, an election, an unknown commit result, or a retry that runs out of `max_retry_time`. Simulated driver failures cover those paths instead. So the durability trade above describes the design, and it is not a tested result.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md),
[key-value backends](../key_value/README.md), and [MongoDB
tests](../../../../tests/backends/test_mongodb_backend.py).
