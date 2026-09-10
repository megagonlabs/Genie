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

## Key layout

Every key starts with `key_prefix`, which defaults to `"genie"`. Several Genie
datasets can share one Redis database by using different prefixes.

| Key | Type | Holds |
| --- | --- | --- |
| `genie:schema` | hash | Application id and schema version |
| `genie:mutations` | string | Token replaced by every committed Genie write |
| `genie:order:blocks`, `genie:order:links` | string | Next stable storage order |
| `genie:block:<id>` | string | One block document |
| `genie:blocks` | sorted set | Every block id in storage order |
| `genie:children:<id>` | sorted set | Direct child ids in creation order |
| `genie:link:<id>` | string | One link document |
| `genie:outlinks:<id>`, `genie:inlinks:<id>` | sorted set | Connected links by endpoint. Each member holds the link id, both endpoint ids, and the relation |
| `genie:relation:<relation>` | sorted set | Stored edges of one relation |
| `genie:acyclic` | set | Relations protected as acyclic graphs |
| `genie:keys:<collection>` | hash | Address-to-block bindings |
| `genie:log:<log>` | sorted set | Log entries scored by sequence |
| `genie:logcounter:<log>` | string | Next sequence for one log |
| `genie:temporal:<collection>` | hash | Temporal entries by block id |
| `genie:temporalindex:<collection>` | sorted set | Block-id order for stable paging |
| `genie:keyowner:<id>`, `genie:logmember:<id>`, `genie:temporalmember:<id>` | set | Reverse indexes used to clean up a deleted block |

## Writes

- **Portable encodings** are used for values, metadata, policies, namespaces, and timestamps, instead of Redis-specific value types.
- **Optimistic concurrency.** The read phase records the value of `genie:mutations`. The commit phase re-checks that value under `WATCH` before it applies every staged change in one `MULTI`/`EXEC` block. A write that would commit on stale reads is retried instead.
- **Same atomic behavior as `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `MongoDBBackend`:** compare-and-swap updates, conditional subtree deletion, unique key bindings, monotonic sequences, and cycle-checked DAG insertion.
- **Every commit writes a fresh token** into `genie:mutations`, so a retry can tell whether its own earlier commit landed.
- **Deleting a block removes the whole subtree** together with its links, key bindings, log entries, and temporal entries in the same commit. Reverse indexes make that cleanup exact instead of a full scan.
- **Link cleanup reads only the endpoint index,** never the link documents, so a link document that is already gone cannot leave its index entries behind.

## Reads

Reads come in three shapes:

| Shape | How it runs | Can it retry? |
| --- | --- | --- |
| Fits in one command | Nothing extra, because Redis runs each command on its own | No |
| Scoped to one block | One read-only Lua script through `EVAL_RO` | No |
| Unbounded in size | Reads the mutation guard before and after its commands | Yes |

- **"Scoped to one block"** means its parent chain, its children, its subtree, or one `limit` page of a collection. Redis runs a script from start to finish before it serves another client, so such a read can never see half of a write.
- **Scripted reads:** `contains`, `ancestors`, `children`, `descendants`, `list_links`, and `list_temporal_entries`, plus the first page of `query_temporal_entries`.
- **`search` splits the same way, by scope.** `Scope.SELF` is one `GET`. `Scope.SUBTREE` and `Scope.ANCESTORS` reuse the scripts behind `descendants` and `ancestors`, so none of the three ever retries. `Scope.GLOBAL` reads every stored block, and `Scope.LINKS` reads every block a link connects to the root, which one block can grow without a bound, so both keep the guard.
- **`list_links` filters on the endpoint index,** because each member carries the relation. Only the matching link documents are read.
- **Script support is checked once during construction** by running a trivial `EVAL_RO`. That tests the capability the reads need instead of guessing from a reported version string. A server older than Redis 7.0, a client without `eval_ro`, and a deployment that blocks scripting all fall back to the guard and its retry.

## Schema and errors

- **Construction checks the schema record** for its application id and version. It refuses a record owned by another application, a version newer than it supports, and a namespace that holds Genie keys with no record. `initialize_schema=False` validates the record without creating it.
- **Client failures map to Genie's stable `BackendError` types.** The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared client.

## Connection health

**The problem:** the backend keeps one client with a connection pool, so a
connection can sit idle between two Genie calls. An idle connection can die
without telling the client — a server `timeout`, a load balancer, a restart, or
a failover closes it — and the next command then fails. The client retries
nothing by default, so that failure would reach the caller as
`BackendUnavailable`.

| Setting | Default | Effect |
| --- | --- | --- |
| `health_check_interval` | 30 seconds | Check an idle connection with a `PING` before reusing it |
| `transport_retries` | 3 | Reconnect and send the command again after the connection drops |

Set either one to `0` to turn it off.

- **A command that timed out on the server is not retried.** It reached the server and then ran past `socket_timeout`, so the server may still be running it and a second attempt would spend the whole timeout again. That case reaches the caller as `BackendBusy`, which is retryable.
- **Opening a connection is retried too,** so plan for it when you pick the two timeouts. A server that refuses the connection answers at once and is tried once. A server that accepts nothing and answers nothing, such as a firewalled address, is tried `transport_retries` more times, and each try may last `connect_timeout`. At the defaults that is four tries of ten seconds before the caller sees `BackendBusy`. Lower `connect_timeout`, `transport_retries`, or both if you need to fail faster.
- **A retry never sends the same write command twice.** Every Genie write runs inside `WATCH`/`MULTI`/`EXEC`, and while a watch is open the client refuses to send those commands again: it reports the dropped connection as a watch conflict instead, and the backend answers that with a fresh attempt that re-reads current state. Every other command the backend sends on its own is a read, so sending one twice changes nothing.
- **One case is left over, and it is not new.** When the connection dies after the server ran `EXEC` but before the client could read the reply, nobody knows whether the write committed. The client reports a watch conflict, and the fresh attempt re-reads current state and usually answers correctly: a repeated `put_block` finds the id taken, and a repeated conditional `update_block` finds the version already raised. `append_log_entry` is the one operation with nothing to compare against, so that exact failure can append a second entry. `Neo4jBackend` and `MongoDBBackend` have the same gap, because a write whose commit reply is lost is retried the same way there.
- **`transport_retries` and `max_retry_time` are separate budgets.** `transport_retries` counts reconnects after the connection drops. `max_retry_time` bounds how long an attempt may keep losing optimistic conflicts to other writers before it raises `BackendBusy`. Neither one covers the other.

## Concurrency

- **Optimistic writes never hold a lock,** so a stalled client cannot block others.
- **The cost is retries.** Every committed write invalidates the attempts running beside it, so throughput drops as write contention rises.
- **`BackendBusy` after `max_retry_time`.** A scanning read that keeps being interrupted by writes reports the same error, because it would rather retry than return a torn view. Both are retryable.

### A retried commit can repeat work

- **Why:** when the connection dies during `EXEC`, the client cannot know whether the server applied the transaction, so it reports a watch conflict either way.
- **The guard token helps first.** Each commit writes a fresh token into the mutation guard, and the next attempt returns its earlier result when the guard still holds that token. The token proves the commit only while no other client has committed since, because the next commit replaces it. Under concurrent writes the operation runs again.
- **Most repeats raise `MemoryConflict`,** because the write checks for its own result first: a block id, a link id, a key address, or a temporal entry that already exists, and any `expected_version` or `expected_block_id` write whose expected value has moved. The write did land, but the caller is told it conflicted.
- **Some repeats end at the same state:** `update_block` without a version, `rebind_key` to the same target, and every cleanup that follows a deleted block.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key, so a repeat appends the same block a second time at a new sequence, and the caller receives the second entry. `Neo4jBackend` and `MongoDBBackend` carry the same limit for the same reason. An application that needs exactly-once appends should carry its own token in the block and skip a duplicate on read.

### Script duration is the cost to watch

- **A scripted read never retries,** so it never reports `BackendBusy` and behaves like a snapshot read on `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, and `MongoDBBackend`.
- **The trade:** a script holds the single-threaded server for as long as it runs, so only a read scoped to one block uses one. A scan of the whole dataset stays on the client side, where it can lose a race but cannot stall every other client.
- **A scripted read moves no extra work onto the server** — the client-side path reads the same data — but that work now runs where every other client is waiting.
- **What makes a script longer:** a very deep ownership chain for `ancestors` and `contains`; a block with very many direct children or connected links for `children` and `list_links`. A `relation` filter narrows `list_links` before it reads any document, but it still walks the whole endpoint index of that block.
- **`descendants` is the longest,** because its work follows the size of the whole subtree rather than one block.
- **So keep ownership trees to a reasonable depth, fan-out, and subtree size,** which is also what makes them useful to read.

## Durability

- **Redis keeps the dataset in memory,** and durability comes from server configuration (RDB snapshots, AOF, or both). The adapter cannot change that, so choose the persistence and replication settings your data needs.
- **Genie never flushes the database,** and it only touches keys under its own prefix.
- **A crash costs recent writes, not a broken dataset.** Every Genie write is one Redis transaction, including a subtree delete, and Redis stores and replicates a transaction as a unit, so a restart brings back whole writes and drops whole writes. A block never comes back with half of its index entries.
- **How much you lose depends on the server:** `appendfsync everysec` loses about one second of writes, `appendfsync no` leaves the timing to the operating system, and RDB snapshots alone lose everything written since the last snapshot. The setting lives on the server, so the adapter neither reads it nor changes it.
- **Replication adds a second way to lose a write that already returned success.** Redis replicates asynchronously, so a failover to a replica that has not received the last transactions drops them. No persistence setting prevents this.
- **The adapter does not use `WAIT` or `WAITAOF`,** which make a write wait for replicas or for the AOF fsync, because both cost a round trip on every write and are not available on every server.

## Server role check

**Point the URL at the primary.**

- **Construction refuses a connection that lands on a replica.** A primary that has replicas passes: the check rejects a replica as the target, not a deployment that has replicas.
- **Writes on a replica already fail loudly** with a read-only error that reaches the caller as `BackendReadOnly`. The silent case is reads: with `initialize_schema=False` a backend on a replica opens, never writes, and serves data that can lag the primary by any amount with no signal.
- **It refuses only on proof.** The check reads `INFO replication` once, next to the eviction check below. A blocked `INFO`, a missing field, and an unrecognized role all pass.
- **`verify_server_role=False`** opens a backend against a replica on purpose, for example a read-only one.
- **Known limits:** the check runs once against the server that answered, so a failover that demotes that server later is not caught, and its writes then fail with `BackendReadOnly`. A proxy that spreads one URL over a primary and its replicas is not caught either. Redis has no equivalent of a bookmark that carries one session's writes into another session's reads, so nothing here makes reads across replicas consistent. Give Genie one primary, and read through the same backend that writes.

## Eviction policy

**The server must not evict Genie keys. Set `maxmemory-policy` to `noeviction`,
which is the Redis default.**

- **Why:** Redis evicts one key at a time, while one Genie block spans a stored document plus several index entries. An eviction can remove a document and leave an index entry pointing at data that is gone.
- **`noeviction` turns an over-limit write into the out-of-memory error instead,** which reaches the caller as `BackendCapacityExceeded`.
- **Construction refuses a server that reports a memory limit together with an `allkeys-*` policy.** The check reads `INFO memory` once, because managed services often block `CONFIG GET`, and it only fails on a reading that proves the server evicts. A blocked `INFO`, a missing field, a `volatile-*` policy (nothing has an expiry to evict), and `maxmemory 0` all pass.
- **`verify_eviction_policy=False`** skips the check and accepts the risk.
- **`SELECT` does not isolate you.** On a self-hosted server `maxmemory-policy` covers the whole process, so a cache workload that needs `allkeys-lru` cannot share the server. Give Genie its own server instead.
- **Plan memory headroom.** Every Genie write ends with `SET` on the mutation guard, and Redis refuses `SET` once `maxmemory` is reached, so at the limit you cannot delete Genie data through the backend to free space.

## Search and temporal limits

- **A subtree read runs inside one script** when scripting is available, and walks one ownership level per round trip otherwise.
- **Portable search reads its candidate blocks through the path its scope allows,** then matches every candidate in Python. Which path a scope takes decides whether that search can fail.

| Scope | Path | Can raise `BackendBusy`? |
| --- | --- | --- |
| `Scope.SELF` | One command | No, atomic whatever the server supports |
| `Scope.SUBTREE`, `Scope.ANCESTORS` | The `descendants` and `ancestors` path | Only without scripting |
| `Scope.GLOBAL`, `Scope.LINKS` | Always the mutation guard | Yes, under sustained writes |

- **`Scope.GLOBAL` reads every stored block,** and `Scope.LINKS` follows an endpoint fan-out that one block can grow without a bound. Neither has a size a script may safely hold the server for.
- **Use a separate search implementation** when you need large full-text or vector indexes.

**Temporal queries scan on the client side as well.**

- **Redis cannot apply time filters server-side,** so `query_temporal_entries` reads the collection in block-id order, a few hundred entries per round trip, and matches each page in Python until it fills `limit`.
- **Plan for a different cost than a query language gives you.** The work follows the size of the collection, not `limit`, and a selective filter over a large collection reads most of it.
- **The first page is cheap** on a server with read-only scripts: it is one atomic script, so a query that ends inside that page never touches the mutation guard and never retries. That covers a collection that fits in one page and any query the first page already fills.
- **Past one page the query falls back to the guarded scan,** and the whole scan is one guarded read, so a Genie write that lands in the middle of it restarts the scan from the start. On a large collection under steady writes the scan can spend its retry budget and raise `BackendBusy`.
- **So keep a temporal collection to a size you are willing to scan,** or let the application split one collection per time range so a query touches only the ranges it needs.

## Deployment target

- **One logical Redis database,** either a standalone server or a primary with replicas. Connect to the primary in the second case, as the role check above requires.
- **Redis Cluster is not a supported target.** `RedisBackend` opens a standalone client with `redis.Redis.from_url`. A standalone client does not follow the `MOVED` and `ASK` replies a cluster node sends for a key it does not hold, so the call fails instead of reaching the right node.
- **Any cluster reply, including `CROSSSLOT`, is reported as `UnsupportedOperation`** rather than as a retryable failure, because retrying cannot fix it.
- **The hash slot rule is worth knowing anyway,** because it is what a cluster would demand. One Redis Cluster transaction may touch only one hash slot, and a script may touch only keys in the slot of the keys the call declares. A Genie write spans many keys, and the read-only scripts build the keys they read from `key_prefix` instead of declaring all of them in `KEYS`.
- **A hash-tagged prefix such as `key_prefix="{genie}"` would meet both rules,** and is still not support: one node would hold the whole dataset, and the backend would stop working as soon as a reshard or a failover moved that slot somewhere else. Use a standalone server or a primary instead.
- **Write through `RedisBackend`.** Genie keys, documents, and indexes belong to the backend. Writing to these keys directly can skip the reference, temporal, and cycle checks, and it does not touch the mutation guard that keeps concurrent Genie writes correct.

## Tests

- **Full suite:** run `../../../scripts/test_full.sh` from the repository root. It starts a primary and one replica that follows it, so the role check is covered against a real `role:slave` server.
- **Your own servers:** live tests need `GENIE_REDIS_URL`. The replica tests also need `GENIE_REDIS_REPLICA_URL` pointing at a replica of that same primary, and skip without it.
- **Optional variables:** `GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_USER`, and `GENIE_REDIS_PASSWORD` choose the test key namespace and the authentication, and apply to both servers.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md),
[document backends](../document/README.md), and [Redis
tests](../../../../tests/backends/test_redis_backend.py).
