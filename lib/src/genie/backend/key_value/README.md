# Redis backend

`RedisBackend` is a persistent key-value adapter on the official `redis` Python
client, with no other layer. It implements the core backend contract, all
optional structure capabilities, and `RouteCatalog`.

```bash
python -m pip install -e ".[redis]"
```

```python
from genie import Memory, RedisBackend


with RedisBackend("redis://localhost:6379/0") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

## Key layout

Each key starts with `key_prefix`, which defaults to `"genie"`. Several Genie
datasets can share one Redis database, with a different prefix for each one.

| Key | Type | Holds |
| --- | --- | --- |
| `genie:schema` | hash | Application id and schema version |
| `genie:mutations` | string | Token that each committed Genie write replaces |
| `genie:order:blocks`, `genie:order:links` | string | Next stable storage order |
| `genie:block:<id>` | string | One block document |
| `genie:blocks` | sorted set | All block ids in storage order |
| `genie:children:<id>` | sorted set | Direct child ids in creation order |
| `genie:link:<id>` | string | One link document |
| `genie:outlinks:<id>`, `genie:inlinks:<id>` | sorted set | Connected links by endpoint. Each member holds the link id, both endpoint ids, and the relation |
| `genie:relation:<relation>` | sorted set | Stored edges of one relation |
| `genie:acyclic` | set | Relations protected as acyclic graphs |
| `genie:keys:<collection>` | hash | Address-to-block bindings |
| `genie:log:<log>` | sorted set | Log entries with the sequence as score |
| `genie:logcounter:<log>` | string | Next sequence for one log |
| `genie:temporal:<collection>` | hash | Temporal entries by block id |
| `genie:temporalindex:<collection>` | sorted set | Block-id order for stable pages |
| `genie:keyowner:<id>`, `genie:logmember:<id>`, `genie:temporalmember:<id>` | set | Reverse indexes for the cleanup of a deleted block |
| `genie:routes` | hash | `RouteCatalog` entries, from block id to route key |

## Writes

- **Portable encodings:** values, metadata, policies, namespaces, and timestamps use portable encodings, not Redis-specific value types.
- **Optimistic concurrency.** The read phase records the value of `genie:mutations`. The commit phase checks that value again under `WATCH`, and then it applies all staged changes in one `MULTI`/`EXEC` block. If the reads are stale, the backend retries the write and does not commit it.
- **Same atomic behavior as `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `MongoDBBackend`:** compare-and-swap updates, conditional subtree delete, unique key bindings, monotonic sequences, and DAG inserts with a cycle check.
- **Each commit writes a new token** into `genie:mutations`. So a retry can tell whether its own earlier commit landed.
- **A block delete removes the whole subtree,** together with its links, key bindings, log entries, and temporal entries, in the same commit. Reverse indexes make that cleanup exact, with no full scan.
- **Link cleanup reads only the endpoint index,** never the link documents. So a link document that is already gone cannot leave its index entries behind.
- **Route catalog entries are separate from blocks.** `claim_route`, `get_route`, and `release_routes` use only `genie:routes`, and a block write or delete does not change that hash. So one namespace can hold the catalog of a `RoutedBackend` and also be one of its routes.
- **A claim is one commit.** It checks the field and writes it under the mutation guard. So of concurrent claims for one block id, also from other backend instances, only one succeeds. `release_routes` removes all given ids in one commit, and an empty list sends no command.

## Reads

Reads come in 3 shapes:

| Shape | How it runs | Can it retry? |
| --- | --- | --- |
| Fits in one command | Nothing extra, because Redis runs each command on its own | No |
| Scoped to one block | One read-only Lua script through `EVAL_RO` | No |
| Unbounded in size | Reads the mutation guard before and after its commands | Yes |

- **"Scoped to one block"** means its parent chain, its children, its subtree, or one `limit` page of a collection. Redis runs a script from start to finish before it serves another client. So such a read can never see half of a write.
- **Script calls:** each call first sends `EVALSHA_RO` with the script digest. If the server lost its script cache, the call sends the script body with `EVAL_RO`.
- **Scripted reads:** `contains`, `ancestors`, `children`, `descendants`, `list_links`, and `list_temporal_entries`, and also the first page of `query_temporal_entries`.
- **`search` uses the same split, by scope.** `Scope.SELF` is one `GET`. `Scope.SUBTREE` and `Scope.ANCESTORS` reuse the scripts behind `descendants` and `ancestors`. So when scripting is available, none of the 3 retries. `Scope.GLOBAL` reads all stored blocks, and `Scope.LINKS` reads the root and each block that a link connects to it, which one block can grow with no limit. So both keep the guard.
- **`list_links` filters on the endpoint index,** because each member carries the relation. The backend reads only the matching link documents.
- **Construction checks script support once** with a trivial `EVAL_RO`. That tests the capability that the reads need, and it does not guess from a reported version string. A server older than Redis 7.0, a client without `eval_ro`, and a deployment that blocks scripting all fall back to the guard and its retry.
- **`list_keys` reads the whole collection hash** with `HGETALL` for each page, then sorts and pages it in Python. So the cost of one page follows the size of the collection.

## Schema and errors

- **Construction checks the schema record** for its application id and version. It refuses a record that another application owns, a version newer than it supports, and a namespace that holds Genie keys with no record. `initialize_schema=False` validates the record, but it does not create it.
- **Client failures map to stable Genie error types:** a `BackendError` subclass, or `UnsupportedOperation` for a cluster reply. The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared client.

## Connection health

**The problem:** the backend keeps one client with a connection pool, so a
connection can stay idle between 2 Genie calls. An idle connection can close
without a signal to the client: a server `timeout`, a load balancer, a restart,
or a failover closes it. The next command then fails. By default, the client
retries nothing, so that failure gets to the caller as `BackendUnavailable`.

| Setting | Default | Effect |
| --- | --- | --- |
| `health_check_interval` | 30 seconds | Check an idle connection with a `PING` before reusing it |
| `transport_retries` | 3 | Reconnect and send the command again after the connection drops |

A value of `0` turns off either setting.

- **A command that timed out on the server is not retried.** It got to the server and then ran past `socket_timeout`. So the server can still run it, and a second attempt would use the whole timeout again. That case gets to the caller as `BackendBusy`, which is retryable.
- **The client also retries a connection open.** This affects the choice of the 2 timeouts. A server that refuses the connection answers at once, and the client tries it once. A server that accepts nothing and answers nothing, such as a firewalled address, gets `transport_retries` more tries, and each try can last `connect_timeout`. At the defaults, that is 4 tries of 10 seconds before the caller sees `BackendBusy`. A lower `connect_timeout`, a lower `transport_retries`, or both make the call fail faster.
- **A retry never sends the same write command twice.** Each Genie write runs inside `WATCH`/`MULTI`/`EXEC`. While a watch is open, the client does not send those commands again. It reports the dropped connection as a watch conflict, and the backend answers with a new attempt that reads the current state again. All other commands that the backend sends on their own are reads, so a second send changes nothing.
- **One case is left over, and it is not new.** If the connection closes after the server ran `EXEC` but before the client read the reply, nobody knows whether the write committed. The client reports a watch conflict. The new attempt reads the current state again and usually answers correctly: a repeated `put_block` finds the id taken, and a repeated conditional `update_block` finds the version already raised. `append_log_entry` is the one operation with nothing to compare against, so that exact failure can append a second entry. `Neo4jBackend` and `MongoDBBackend` have the same gap, because they retry a write with a lost commit reply in the same way.
- **`transport_retries` and `max_retry_time` are separate budgets.** `transport_retries` counts reconnects after the connection drops. `max_retry_time` limits how long an attempt can continue to lose optimistic conflicts to other writers before it raises `BackendBusy`. Neither one covers the other.

## Concurrency

- **Optimistic writes never hold a lock,** so a stalled client cannot block others.
- **The cost is retries.** Each committed write makes the attempts that run beside it stale. So throughput drops as write contention increases.
- **`BackendBusy` after `max_retry_time`.** A scan read that writes interrupt again and again reports the same error, because a retry is better than a torn view. Both errors are retryable.

### A retried commit can repeat work

- **Why:** when the connection closes during `EXEC`, the client cannot know whether the server applied the transaction. So it reports a watch conflict in both cases.
- **The guard token helps first.** Each commit writes a new token into the mutation guard. The next attempt returns its earlier result when the guard still holds that token. The token proves the commit only while no other client has committed since, because the next commit replaces it. Under concurrent writes, the operation runs again.
- **Most repeats raise `MemoryConflict`,** because the write first checks for its own result: a block id, a link id, a key address, a route catalog entry, or a temporal entry that already exists, a temporal entry that is already removed, and each `expected_version`, `expected_blocks`, or `expected_block_id` write whose expected value has moved. The write did land, but the caller gets a conflict.
- **A repeated `claim_route` keeps its entry.** The caller gets `MemoryConflict`, and the entry stays until `release_routes` removes it. Through `RoutedBackend`, that put fails, and later puts of the id also raise `MemoryConflict` until the release.
- **Some repeats end at the same state:** `update_block` without a version, `rebind_key` to the same target, `replace_temporal_entry`, `update_link`, `release_routes`, and each cleanup after a deleted block.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key. So a repeat appends the same block a second time at a new sequence, and the caller gets the second entry. `Neo4jBackend` and `MongoDBBackend` have the same limit for the same reason. For exactly-once appends, an application keeps its own token in the block and skips a duplicate on read.

### Script duration is the cost to watch

- **A scripted read never retries.** So it never reports `BackendBusy` for a lost race, and it acts like a snapshot read on `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `MongoDBBackend`. A command timeout still reports `BackendBusy`.
- **The trade:** a script holds the single-threaded server for as long as it runs. So only a read scoped to one block uses one. A scan of the whole dataset stays on the client side, where it can lose a race but cannot stall all other clients.
- **A scripted read moves no extra work onto the server.** The client-side path reads the same data. But that work now runs where all other clients wait.
- **What makes a script longer:** a very deep ownership chain for `ancestors` and `contains`, and a block with very many direct children or connected links for `children` and `list_links`. A `relation` filter narrows `list_links` before it reads a document, but it still walks the whole endpoint index of that block.
- **`descendants` is the longest,** because its work follows the size of the whole subtree, not of one block.
- **So ownership trees work best with a reasonable depth, fan-out, and subtree size,** which also makes them useful to read.

## Durability

- **Redis keeps the dataset in memory.** Durability comes from the server configuration: RDB snapshots, AOF, or both. The adapter cannot change that, so the persistence and replication settings come from what your data needs.
- **Genie never flushes the database,** and it only touches keys under its own prefix.
- **A crash costs recent writes, not a broken dataset.** Each Genie write is one Redis transaction, also a subtree delete. Redis stores and replicates a transaction as a unit, so a restart brings back whole writes and drops whole writes. A block never comes back with half of its index entries.
- **How much you lose depends on the server:** `appendfsync everysec` loses about one second of writes, `appendfsync no` leaves the timing to the operating system, and RDB snapshots alone lose all writes since the last snapshot. The setting is on the server, so the adapter does not read it and does not change it.
- **Replication adds a second way to lose a write that already returned success.** Redis replicates asynchronously. So a failover to a replica that did not get the last transactions drops them. No persistence setting prevents this.
- **The adapter does not use `WAIT` or `WAITAOF`.** They make a write wait for replicas or for the AOF fsync. Both cost a round trip on each write, and not all servers have them.

## Server role check

**A supported URL points at the primary.**

- **Construction refuses a connection that lands on a replica.** A primary that has replicas passes: the check rejects a replica as the target, not a deployment that has replicas.
- **Writes on a replica already fail loudly** with a read-only error that gets to the caller as `BackendReadOnly`. The silent case is reads: with `initialize_schema=False`, a backend on a replica opens, never writes, and serves data that can lag the primary by any amount with no signal.
- **It refuses only on proof.** The check reads `INFO replication` once, before the eviction check below. A blocked `INFO`, a missing field, and an unrecognized role all pass.
- **`verify_server_role=False`** opens a backend against a replica on purpose, for example a read-only one.
- **Known limits:** the check runs once against the server that answered. So a failover that demotes that server later is not caught, and its writes then fail with `BackendReadOnly`. A proxy that spreads one URL over a primary and its replicas is not caught either. Redis has no equivalent of a bookmark that carries the writes of one session into the reads of another session, so nothing here makes reads across replicas consistent. The supported setup is one primary for Genie, with reads through the same backend that writes.

## Eviction policy

**The server does not evict Genie keys when `maxmemory-policy` is `noeviction`,
which is the Redis default.**

- **Why:** Redis evicts one key at a time, while one Genie block spans a stored document plus several index entries. An eviction can remove a document and leave an index entry that points at data that is gone.
- **`noeviction` turns an over-limit write into the out-of-memory error instead,** which gets to the caller as `BackendCapacityExceeded`.
- **Construction refuses a server that reports a memory limit together with an `allkeys-*` policy.** The check reads `INFO memory` once, because managed services often block `CONFIG GET`. It fails only on a reading that proves that the server evicts. A blocked `INFO`, a missing field, a `volatile-*` policy (nothing has an expiry to evict), and `maxmemory 0` all pass.
- **`verify_eviction_policy=False`** skips the check and accepts the risk.
- **`SELECT` does not isolate you.** On a self-hosted server, `maxmemory-policy` covers the whole process. So a cache workload that needs `allkeys-lru` cannot share the server. Genie gets its own server instead.
- **Memory headroom is necessary.** Each Genie write ends with `SET` on the mutation guard, and Redis refuses `SET` when it reaches `maxmemory`. So at the limit, you cannot delete Genie data through the backend to free space.

## Search and temporal limits

- **A subtree read runs inside one script** when scripting is available. If not, it walks one ownership level for each round trip.
- **Portable search reads its candidate blocks through the path that its scope allows,** then matches each candidate in Python. The path of a scope decides whether that search can fail.

| Scope | Path | Can raise `BackendBusy`? |
| --- | --- | --- |
| `Scope.SELF` | One command | No, atomic whatever the server supports |
| `Scope.SUBTREE`, `Scope.ANCESTORS` | The `descendants` and `ancestors` path | Only without scripting |
| `Scope.GLOBAL`, `Scope.LINKS` | Always the mutation guard | Yes, under sustained writes |

- **`Scope.GLOBAL` reads all stored blocks,** and `Scope.LINKS` follows an endpoint fan-out that one block can grow with no limit. Neither has a size for which a script can safely hold the server.
- **Large full-text or vector indexes need a separate search implementation.**

**Temporal queries also scan on the client side.**

- **Redis cannot apply time filters on the server.** So `query_temporal_entries` reads the collection in block-id order, 500 entries for each round trip, and matches each page in Python until it fills `limit`.
- **The cost is different from the cost of a query language.** The work follows the size of the collection, not `limit`, and a selective filter over a large collection reads most of it.
- **The first page is cheap** on a server with read-only scripts. It is one atomic script, so a query that ends inside that page never touches the mutation guard and never retries. That covers a collection that fits in one page, and each query that the first page already fills.
- **After one page, the query falls back to the guarded scan.** The whole scan is one guarded read, so a Genie write in the middle of it restarts the scan from the start. On a large collection under steady writes, the scan can use up its retry budget and raise `BackendBusy`.
- **So a temporal collection works best at a size that you accept for a full scan.** Or the application can split one collection for each time range, so that a query touches only the ranges that it needs.

## Deployment target

- **One logical Redis database:** a standalone server or a primary with replicas. In the second case, the backend connects to the primary, as the role check above requires.
- **Redis Cluster is not a supported target.** `RedisBackend` opens a standalone client with `redis.Redis.from_url`. A standalone client does not follow the `MOVED` and `ASK` replies that a cluster node sends for a key that it does not hold. So the call fails and does not get to the correct node.
- **Each cluster reply, also `CROSSSLOT`, is reported as `UnsupportedOperation`,** not as a retryable failure, because a retry cannot fix it.
- **The hash slot rule is useful to know,** because a cluster would require it. One Redis Cluster transaction can touch only one hash slot, and a script can touch only keys in the slot of the keys that the call declares. A Genie write spans many keys, and the read-only scripts build the keys that they read from `key_prefix`. They do not declare all of them in `KEYS`.
- **A hash-tagged prefix such as `key_prefix="{genie}"` would meet both rules,** but it is still not support. One node would hold the whole dataset, and the backend would stop when a reshard or a failover moved that slot to a different node. The supported targets are a standalone server or a primary.
- **Only `RedisBackend` writes the Genie keys safely.** Genie keys, documents, and indexes belong to the backend. A direct write to these keys can skip the reference, temporal, and cycle checks, and it does not touch the mutation guard that keeps concurrent Genie writes correct.

## Tests

- **Full suite:** `lib/scripts/test_full.sh`, run from the repository root, starts disposable server containers, Redis among them, and runs the complete suite. For Redis, it starts a primary and one replica that follows it, so the tests cover the role check against a real `role:slave` server.
- **Your own servers:** live tests need `GENIE_REDIS_URL`. The replica tests also need `GENIE_REDIS_REPLICA_URL`, which points at a replica of that same primary. Without it, they skip.
- **Optional variables:** `GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_USER`, and `GENIE_REDIS_PASSWORD` set the test key namespace and the authentication. They apply to both servers.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md),
[document backends](../document/README.md), and [Redis
tests](../../../../tests/backends/test_redis_backend.py).
