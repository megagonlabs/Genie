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

## Behavior

- Values, metadata, policies, namespaces, and timestamps use strict portable
  encodings instead of Redis-specific value types.
- Writes use optimistic concurrency. The read phase records the value of
  `genie:mutations`, and the commit phase re-checks that value under `WATCH`
  before it applies every staged change in one `MULTI`/`EXEC` block. A write
  that would commit on stale reads is retried instead. This gives the same
  atomic behavior as SQLite and Neo4j: compare-and-swap updates, conditional
  subtree deletion, unique key bindings, monotonic sequences, and cycle-checked
  DAG insertion. Each commit writes a fresh token into `genie:mutations`, so a
  retry can tell whether its own earlier commit landed.
- Reads come in three shapes. A read that fits in one command needs nothing
  extra, because Redis runs each command on its own. A read scoped to one block,
  meaning its parent chain, its children, its subtree, or one `limit` page of a
  collection, runs as one read-only Lua script through `EVAL_RO`. Redis runs a
  script from start to finish before it serves another client, so such a read can
  never see half of a write and never retries. `contains`, `ancestors`,
  `children`, `descendants`, `list_links`, and `list_temporal_entries` use that
  path, and so does the first page of `query_temporal_entries`. A read with no
  bound on its size reads the guard before and after its commands and retries
  when a write landed in the middle.
- `search` splits the same way, by scope. `Scope.SELF` is one `GET`, and
  `Scope.SUBTREE` and `Scope.ANCESTORS` reuse the scripts behind `descendants`
  and `ancestors`, so none of the three ever retries. `Scope.GLOBAL` reads every
  stored block, and `Scope.LINKS` reads every block a link connects to the root,
  which one block can grow without a bound, so both keep the guard.
- Read-only script support is checked once during construction by running a
  trivial `EVAL_RO`. That tests the capability the reads need instead of
  guessing from a reported version string. A server older than Redis 7.0, a
  client without `eval_ro`, and a deployment that blocks scripting all fall back
  to the guard and its retry.
- Deleting a block removes the whole subtree together with its links, key
  bindings, log entries, and temporal entries in the same commit. Reverse
  indexes make that cleanup exact instead of a full scan. Link cleanup reads
  only the endpoint index, never the link documents, so a link document that is
  already gone cannot leave its index entries behind.
- `list_links` applies its `relation` filter to the endpoint index, because each
  member carries the relation. Only the matching link documents are read.
- On construction the backend checks the schema record for its application id
  and version. It refuses a record owned by another application, a version
  newer than it supports, and a namespace that holds Genie keys with no record.
  `initialize_schema=False` validates the record without creating it.
- Client failures are mapped to Genie's stable `BackendError` types. The
  original error stays available as `__cause__`.
- `close()` waits for active operations before it closes the shared client.

## Connection health

The backend keeps one client with a connection pool, so a connection can sit
idle between two Genie calls. An idle connection can die without telling the
client: a server `timeout`, a load balancer, a restart, or a failover closes
it, and the next command then fails. The client retries nothing by default, so
that failure would reach the caller as `BackendUnavailable`.

Two settings cover this. `health_check_interval`, 30 seconds by default, makes
the client check an idle connection with a `PING` before it reuses it.
`transport_retries`, 3 by default, lets the client reconnect and send the
command again after the connection drops. Set either one to `0` to turn it off.

A command that reached the server and then ran past `socket_timeout` is not
retried, because the server may still be running it and a second attempt would
spend the whole timeout again. That case reaches the caller as `BackendBusy`,
which is retryable.

Opening a connection is retried as well, so plan for it when you pick the two
timeouts. A server that refuses the connection answers at once and is tried
once. A server that accepts nothing and answers nothing, such as a firewalled
address, is tried `transport_retries` more times, and each try may last
`connect_timeout`. At the defaults that is four tries of ten seconds before the
caller sees `BackendBusy`. Lower `connect_timeout`, `transport_retries`, or
both if you need to fail faster than that.

A retry never sends the same write command twice. Every Genie write runs inside
`WATCH`/`MULTI`/`EXEC`, and while a watch is open the client refuses to send
those commands again: it reports the dropped connection as a watch conflict
instead, and the backend answers that with a fresh attempt that re-reads
current state. Every other command the backend sends on its own is a read, so
sending one twice changes nothing.

One case is left over, and it is not new. When the connection dies after the
server ran `EXEC` but before the client could read the reply, nobody knows
whether the write committed. The client reports a watch conflict, and the fresh
attempt re-reads current state and usually answers correctly: a repeated
`put_block` finds the id taken, and a repeated conditional `update_block` finds
the version already raised. `append_log_entry` is the one operation with
nothing to compare against, so that exact failure can append a second entry.
`Neo4jBackend` has the same gap, because a managed transaction whose commit
reply is lost is retried the same way.

`transport_retries` and `max_retry_time` are separate budgets.
`transport_retries` counts reconnects after the connection drops.
`max_retry_time` bounds how long an attempt may keep losing optimistic
conflicts to other writers before it raises `BackendBusy`. Neither one covers
the other.

## Concurrency and limits

Optimistic writes never hold a lock, so a stalled client cannot block others.
The cost is retries: every committed write invalidates the attempts running
beside it, so throughput drops as write contention rises. A write that cannot
commit inside `max_retry_time` raises `BackendBusy`, which is retryable. A
scanning read that keeps being interrupted by writes reports the same error,
because it would rather retry than return a torn view.

A commit whose reply never arrives is retried, and the retry can repeat work.
When the connection dies during `EXEC`, the client cannot know whether the
server applied the transaction, so it reports a watch conflict either way. Each
commit therefore writes a fresh token into the mutation guard, and the next
attempt returns its earlier result when the guard still holds that token. The
token proves the commit only while no other client has committed since, because
the next commit replaces it. Under concurrent writes the operation runs again.

Running it again is safe for most writes and visible for one. A write that
checks for its own result first raises `MemoryConflict` on the repeat: a block
id, a link id, a key address, or a temporal entry that already exists, and any
`expected_version` or `expected_block_id` write whose expected value has moved.
The write did land, but the caller is told it conflicted. A write with no such
check ends at the same state: `update_block` without a version, `rebind_key` to
the same target, and every cleanup that follows a deleted block.
`append_log_entry` is the exception. It allocates the next sequence with no
uniqueness key, so a repeat appends the same block a second time at a new
sequence, and the caller receives the second entry. Neo4j's managed
transactions carry the same limit for the same reason. An application that
needs exactly-once appends should carry its own token in the block and skip a
duplicate on read.

A read that runs as one read-only script never enters a retry loop, so it never
reports `BackendBusy` and behaves like a snapshot read on SQLite or Neo4j. The
trade is that a script holds the single-threaded server for as long as it runs,
so only a read scoped to one block uses one. A scan of the whole dataset stays
on the client side, where it can lose a race but cannot stall every other
client.

Script duration is the cost to watch. A scripted read moves no extra work onto
the server, because the client-side path reads the same data, but that work now
runs where every other client is waiting. A very deep ownership chain makes
`ancestors` and `contains` longer, and a block with very many direct children or
connected links makes `children` and `list_links` longer. A `relation` filter
narrows `list_links` before it reads any document, but it still walks the whole
endpoint index of that block. `descendants` is the
longest of them, because its work follows the size of the whole subtree rather
than one block. Keep ownership trees to a reasonable depth, fan-out, and subtree
size, which is also what makes them useful to read.

Redis keeps the dataset in memory, and durability comes from server
configuration (RDB snapshots, AOF, or both). The adapter cannot change that, so
choose the persistence and replication settings your data needs. Genie never
flushes the database, and it only touches keys under its own prefix.

A crash costs recent writes, not a broken dataset. Every Genie write is one
Redis transaction, including a subtree delete, and Redis stores and replicates
a transaction as a unit, so a restart brings back whole writes and drops whole
writes. A block never comes back with half of its index entries. How much you
lose depends on the server: `appendfsync everysec` loses about one second of
writes, `appendfsync no` leaves the timing to the operating system, and RDB
snapshots alone lose everything written since the last snapshot. The setting
lives on the server, so the adapter neither reads it nor changes it.

Replication adds a second way to lose a write that already returned success.
Redis replicates asynchronously, so a failover to a replica that has not
received the last transactions drops them. No persistence setting prevents
this. The adapter does not use `WAIT` or `WAITAOF`, which make a write wait for
replicas or for the AOF fsync, because both cost a round trip on every write
and are not available on every server.

Point the URL at the primary. Construction refuses a connection that lands on a
replica, and a primary that has replicas passes; the check rejects a replica as
the target, not a deployment that has replicas. A replica already refuses every
write with a read-only error that reaches the caller as `BackendReadOnly`, so
the silent case is reads: with `initialize_schema=False` a backend on a replica
opens, never writes, and serves data that can lag the primary by any amount with
no signal. The check reads `INFO replication` once, next to the eviction check
below, and only fails on a reading that proves the server is a replica. A
blocked `INFO`, a missing field, and an unrecognized role all pass. Pass
`verify_server_role=False` to open a backend against a replica on purpose, for
example a read-only one.

The check has limits worth knowing. It runs once against the server that
answered, so a failover that demotes that server later is not caught, and its
writes then fail with `BackendReadOnly`. A proxy that spreads one URL over a
primary and its replicas is not caught either. Redis has no equivalent of a
bookmark that carries one session's writes into another session's reads, so
nothing here makes reads across replicas consistent. Give Genie one primary, and
read through the same backend that writes.

The server must also not evict Genie keys. Set `maxmemory-policy` to
`noeviction`, which is the Redis default. Redis evicts one key at a time, while
one Genie block spans a stored document plus several index entries, so an
eviction can remove a document and leave an index entry pointing at data that is
gone. `noeviction` turns an over-limit write into the out-of-memory error
instead, which reaches the caller as `BackendCapacityExceeded`.

Construction refuses a server that reports a memory limit together with an
`allkeys-*` policy. The check reads `INFO memory` once, because managed services
often block `CONFIG GET`, and it only fails on a reading that proves the server
evicts. A blocked `INFO`, a missing field, a `volatile-*` policy (nothing has an
expiry to evict), and `maxmemory 0` all pass. Pass
`verify_eviction_policy=False` to skip the check and accept the risk. On a
self-hosted server `maxmemory-policy` covers the whole process, so `SELECT` does
not isolate Genie from a cache workload that needs `allkeys-lru`; give Genie its
own server instead. Plan memory headroom as well: every Genie write ends with
`SET` on the mutation guard, and Redis refuses `SET` once `maxmemory` is
reached, so at the limit you cannot delete Genie data through the backend to
free space.

A subtree read runs inside one script when scripting is available, and walks one
ownership level per round trip otherwise. Portable search reads its candidate
blocks through the path its scope allows, then matches every candidate in
Python. Which path a scope takes decides whether that search can fail.
`Scope.SELF` reads one block with one command, so it is atomic whatever the
server supports. `Scope.SUBTREE` and `Scope.ANCESTORS` read one subtree or one
parent chain, so they take the same path as `descendants` and `ancestors`: one
atomic script where scripting is available, and the mutation guard otherwise.
`Scope.GLOBAL` reads every stored block, and `Scope.LINKS` follows an endpoint
fan-out that one block can grow without a bound. Neither has a size a script may
safely hold the server for, so both always keep the guard and can raise
`BackendBusy` under sustained writes. Use a separate search implementation when
you need large full-text or vector indexes.

Temporal queries scan on the client side as well. Redis cannot apply time
filters server-side, so `query_temporal_entries` reads the collection in
block-id order, a few hundred entries per round trip, and matches each page in
Python until it fills `limit`. A store that can express these filters in its
own query language reads only matching rows, so plan for a different cost here:
the work follows the size of the collection, not `limit`, and a selective
filter over a large collection reads most of it.

How far that cost hurts depends on how many pages the query needs. On a server
with read-only scripts the first page is one atomic script, so a query that
ends inside that page never touches the mutation guard and never retries. That
covers a collection that fits in one page and any query the first page already
fills. Past one page the query falls back to the guarded scan, and the whole
scan is one guarded read, so a Genie write that lands in the middle of it
restarts the scan from the start. On a large collection under steady writes the
scan can spend its retry budget and raise `BackendBusy`. Keep a temporal
collection to a size you are willing to scan, or let the application split one
collection per time range so a query touches only the ranges it needs.

The adapter targets one logical Redis database, which can be a standalone
server or a primary with replicas. Connect it to the primary in the second
case, as the role check above requires.

Redis Cluster is not a supported target. `RedisBackend` opens a standalone
client with `redis.Redis.from_url`. A standalone client does not follow the
`MOVED` and `ASK` replies a cluster node sends for a key it does not hold, so
the call fails instead of reaching the right node. Any cluster reply, including
`CROSSSLOT`, is reported as `UnsupportedOperation` rather than as a retryable
failure, because retrying cannot fix it.

The hash slot rule is worth knowing anyway, because it is what a cluster would
demand. One Redis Cluster transaction may touch only one hash slot, and a
script may touch only keys in the slot of the keys the call declares. A Genie
write spans many keys, and the read-only scripts build the keys they read from
`key_prefix` instead of declaring all of them in `KEYS`. A hash-tagged prefix
such as `key_prefix="{genie}"` would place every Genie key in one slot and meet
both rules. That is still not support: one node would hold the whole dataset,
and the backend would stop working as soon as a reshard or a failover moved
that slot somewhere else. Use a standalone server or a primary instead.

Genie keys, documents, and indexes belong to the backend. Write through
`RedisBackend`. Writing to these keys directly can skip the reference,
temporal, and cycle checks, and it does not touch the mutation guard that keeps
concurrent Genie writes correct.

Run `../../../scripts/test_full.sh` from the repository root to start throwaway
Redis containers and run the whole suite. The script starts a primary and one
replica that follows it, so the role check is covered against a real
`role:slave` server. If you run the servers yourself, live tests need
`GENIE_REDIS_URL`, and the replica tests also need `GENIE_REDIS_REPLICA_URL`
pointing at a replica of that same primary. Without it those tests skip. The
optional `GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_USER`, and
`GENIE_REDIS_PASSWORD` variables choose the test key namespace and the
authentication, and apply to both servers.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [graph backends](../graph/README.md), and
[Redis tests](../../../tests/backends/test_redis_backend.py).
