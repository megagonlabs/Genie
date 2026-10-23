# Benchmarks

This harness measures the latency of the raw memory operations of Genie. It
measures one store at a time, at 2 API layers, with one caller or with more than
one caller.

- **Questions that it answers:** the cost of one operation, the part of that
  cost that comes from Genie and not from the store below it, and the changes
  when more than one caller works at the same time.
- **Purpose:** it keeps baseline numbers for comparison with a later release.
  Thus you can see a slowdown before users find it.
- **Not a test suite.** The harness has no assertions. `pytest` does not
  collect this directory, so a normal test run stays fast.
  `lib/tests/test_benchmarks.py` checks that the harness itself works.
- **Not a comparison with other memory libraries.** Those libraries call a
  model before they write. So their write latency measures a model provider,
  not the code of a memory system.
- **No extra packages.** The harness uses only the standard library. The core
  package follows the same rule.

## Run

Run these commands from the repository root:

```bash
python lib/benchmarks/run.py
python lib/benchmarks/run.py --list
python lib/benchmarks/run.py --iterations 500 --blocks 5000
python lib/benchmarks/run.py --target sqlite --group read
python lib/benchmarks/run.py --layer backend --layer memory
python lib/benchmarks/run.py --workers 4 --workers 8
python lib/benchmarks/run.py --workload --workers 4
python lib/benchmarks/run.py --json reports/latest.json
```

- **A default run takes approximately 30 seconds.** It needs no server and no
  extra package.
- **The exit status is `1` if a measured call raised an exception,** and `0`
  if not. The duration of a call is never a pass or fail signal, because the
  noise on a shared machine is too large. A conflict that a `contention` case
  expects is not an error.
- **The exit status is `2` for a usage error,** for example an unknown target,
  a selection that matches no case, or a flag that `--workload` does not
  accept.
- **`--workers` is the only concurrency switch.** Without it, the run measures
  only one caller, and the output has no concurrency table.
- **`--workload` measures the mixed read/write scenario instead of the cases.**
  It is the only flag that changes the structure of a run and not only its
  size.
- **`--quiet` removes the progress lines and the tables.** This is useful
  together with `--json`.

## The three layers

Each case declares the layers at which the harness can measure it. This split
is the main purpose of the suite.

| Layer | What it measures |
| --- | --- |
| `backend` | One call on a backend capability protocol. No policy check, no hook dispatch, no codec, and no block copy |
| `memory` | The equivalent call on the public `Memory` or `MemoryHandle` API. This is the cost that an application pays |
| `core` | One core-library helper alone, with no store behind it |

- **The overhead table shows the gap between `backend` and `memory`.** It
  shows the cost that Genie adds to the store cost for the same logical
  operation.
- **The `core` cases explain that gap.** They measure the codec, the
  `PolicyEngine`, and the hook envelope separately. So you can find the cause
  of a large gap, and you do not have to guess it.
- **`core` runs one time, in its own section.** A core helper has no store
  behind it, so a repeat under each target gives only the same number again.
  `--target` has no effect on it. `--layer core` and `--group overhead` select
  it.


## Concurrency

`--workers` measures the same case with more than one caller at the same time.
It is off by default, so a plain run keeps its duration and its numbers.

```bash
python lib/benchmarks/run.py --workers 4 --workers 8
python lib/benchmarks/run.py --group contention --workers 4
```

- **The worker count is a third axis,** together with the target and the
  layer. The harness can measure each case at each level. So a search case
  with 8 callers is as available as `hot_block_set`.
- **All workers share one `Memory` and one backend,** as in an application.
  The workers are threads, not processes.
- **`--iterations` and `--warmup` are counts for each worker.** 4 workers make
  4 times as many calls. So you can compare the latency across levels, and the
  throughput can increase.
- **The harness always measures level 1,** because `speedup` divides by it.
  `--workers 8` measures level 1 and level 8.
- **`core` cases run only at level 1.** A core helper has no store behind it,
  so no contention can occur.
- **The levels of one case run one after the other.** So the runs that you
  compare occur close together in time, and a slow change in the load of the
  machine cannot alone change `speedup`.

### The two rates

| Number | What it shows |
| --- | --- |
| `ops_per_second` | The rate that one caller saw, from the duration of its own calls |
| `throughput_per_second` | The rate of all callers together, from the wall time of the measured window |
| `speedup` | `throughput_per_second` divided by the `throughput_per_second` of a single caller |

- **A speedup near 1 means that the store serializes the calls.** More callers
  do not complete more work. They wait in a queue for the same turn.
- **A speedup below 1 is normal for an adapter that serializes.** The callers
  do the same serialized work, and they also pay for the lock handoff.
- **Here, latency and throughput change in opposite directions.** A caller in
  a queue waits longer. So `p50_ms` increases, and `p95_ms` increases more. But
  the store completes approximately the same number of calls each second. Read
  both columns.

### Conflicts

- **A conflict is a lost race, not a failure.** `Memory.set` and
  `Memory.update` read a block, and then write it with `expected_version`. If
  another worker wrote the block first, the call raises `MemoryConflict`.
- **The harness counts conflicts separately from errors,** in their own
  column. Conflicts never change the exit status. Each case declares the
  exception types of the races that it expects to lose. All other exceptions
  are errors.
- **The harness does not time a call that had a conflict.** The call did not
  complete its work, so its duration stays out of the percentiles.
  `throughput_per_second` counts only the calls that committed.
- **Genie does not retry.** The conflict goes to the caller, as the contract
  specifies. These cases measure how frequently an application has to retry.

### What each adapter can show

The `speedup` column has a meaning only together with the concurrency model of
the adapter.

| Adapter | In-process model | What a concurrent run shows |
| --- | --- | --- |
| `InMemoryBackend` | Each operation takes one `RLock` | Calls wait in a queue, so the speedup stays near 1 |
| `SQLiteBackend` | One connection. One `RLock` stays held for all of a read or write transaction | All operations serialize |
| `PostgresBackend` | The same, and also `pg_advisory_xact_lock`, so writes also serialize across processes | All operations serialize |
| `MongoDBBackend` | A pooled client. Writes take a guard document in the database | Reads overlap. Writes wait for their turn |
| `Neo4jBackend` | A pooled driver. Writes take a guard in the database | Reads overlap. Writes wait for their turn |
| `RedisBackend` | A pooled client | Reads overlap |

- **These are not defects.** The shipped adapters target one small, controlled
  database. One guarded writer at a time makes their compound "read, check,
  then write" steps atomic.
- **An adapter that serializes still gives useful numbers.** The latency under
  load and the tail show how long a caller waits for its turn. An application
  sees this wait.

### The switch interval decides what a thread benchmark can see

- **CPython runs one thread at a time.** In pure-Python code, it changes to a
  different thread only after `sys.getswitchinterval()` seconds, 5 ms by
  default. Each report records the value as `switch_interval_ms`. This value
  has a larger effect on the conflict counts than all other items in the
  environment.
- **A pure-Python call is much shorter than 5 ms.** So one worker completes
  thousands of `InMemoryBackend` operations in a single turn. Another worker
  almost never runs between its read and its write.
- **Thus `hot_block_set` on `in_memory` reports 0 conflicts,** also with 8
  workers and thousands of writes to one block.
- **This result comes from the interpreter. It does not show a safe backend.**
  In a test, the same 8 workers each wrote the same block 2000 times. With
  `sys.setswitchinterval` set to 1 microsecond, they got more than 12000
  conflicts.
- **`sqlite` and each adapter that connects to a server release the GIL in the
  call.** So their workers really interleave, and their conflict counts are
  correct. `sqlite` gets conflicts on a hot block. `in_memory` does not.
- **Use the `in_memory` concurrency numbers as a lower limit.** Use a server
  target for the conflict behavior.

## Mixed read and write load

`--workload` replaces the single-operation cases with one fixed scenario. In
this scenario, readers and writers use the same blocks at the same time. No
case can answer this question, because all workers of a case make the same
call.

```bash
python lib/benchmarks/run.py --workload --workers 4
python lib/benchmarks/run.py --workload --workers 4 --workers 8 --target sqlite
python lib/benchmarks/run.py --workload --workers 4 --list
```

- **One flag, and no other settings.** The scenario is fixed, so 2 runs
  measure the same thing. It uses the flags `--workers`, `--blocks`,
  `--target`, `--warmup`, `--json`, `--quiet`, and `--list`. The corpus flags
  `--sections`, `--value-size`, and `--seed` also apply.
- **It needs more than one worker.** One worker cannot read and write at the
  same time. So `--workload` alone is a usage error, and the error message
  gives the fix.
- **It measures no cases.** So the run refuses `--layer`, `--group`, and
  `--case`. It does not ignore them without a message.
- **`--iterations`, `--search-limit`, and `--links-per-block` have no effect
  on it.** The run writes this one time to standard error. The window comes
  from `--blocks`, and no stream searches or follows a link. The JSON file
  records all settings.

### The scenario

| Element | Choice |
| --- | --- |
| Split | 3 readers for each writer, from the `--workers` count |
| Readers | `Memory.get`. Each reader goes through the leaf list from its own start offset. At the end of the list, it goes back to the start. It continues until the stop signal |
| Writers | `Memory.set`. The writers divide the leaf list into contiguous ranges. Each writer goes through its own range one time |
| Layer | `memory` only |
| Window | Ends when the last writer completes its part of one sweep |

- **Roles do not change during the window.** A worker only reads or only
  writes. So the read latency comes from threads that only read.
- **The window is one full sweep of the corpus,** not a fixed duration. It
  comes from `--blocks`, and it has a clear meaning. It adapts to a slow store
  or a fast store. It also makes sure that the writers write again each block
  that a reader can read during the window.
- **The writers divide the sweep. They do not repeat it.** With 2 writers,
  each writer takes half of the leaves. So, at each worker count, the writers
  write the corpus again exactly one time, and no 2 writers write the same
  block. More writers give a shorter window and more writes each second.
- **The reader offsets only prevent many readers from a start on the same
  page.** At the end of the list, a reader goes back to the start, and it reads
  all of the corpus many times during one sweep. So, after the first pass, its
  start position has no effect.
- **Warmup writes go to a temporary block that only that writer uses.** This
  block is a root outside the corpus. If not, the first `--warmup` calls would
  use a part of the sweep, and the sweep would not be exactly one pass.

### Why these two operations

- **`Memory.get` and `Memory.set` on leaves that exist are the only pair that
  collides on the same row.** `Memory.put` makes new ids, so it never writes a
  block that a reader reads.
- **`delete` is not in the mix.** After a delete, later reads return `None`,
  and later writes fail with `MemoryNotFound`. The run would then measure the
  path for a block that does not exist, not contention.
- **A raw `backend` write cannot be part of the mix.** `update_block` needs an
  `expected_version`. If a second worker writes the same block, the benchmark
  cannot know that version before the call. `Memory.set` gets the version
  itself. So one write is 2 store round trips, not one.
- **The overlap is certain over the window, but not at one instant.** A read
  can occur just before, just after, or during a write to the same block. The
  scheduler decides which. For a certain collision on one row at the same
  instant, use `--group contention`.

### The three passes

The harness measures each target and worker level 3 times, in this order.

| Pass | What runs | Window |
| --- | --- | --- |
| `mixed` | readers and writers together | ends when the sweep ends |
| `reads_alone` | the readers make the same number of calls as in `mixed`, with no writer | the same calls |
| `writes_alone` | the writers sweep the corpus one time, with no reader | one sweep |

- **The order is fixed.** `reads_alone` uses the call counts from `mixed`, so
  the harness cannot plan it before `mixed` runs. Thus one work item contains
  all 3 passes, and not one variant for each pass.
- **The passes run one after the other.** Each pass has a new store and a new
  corpus. So a slow change in the load of the machine cannot change the
  differences.
- **The passes match call counts, not durations.** So the 2 sides of the
  comparison have the same number of samples.
- **A baseline keeps the same workers in its stream and only stops the other
  stream.** So the difference that it shows is interference, not concurrency.

### Reading the table

- **`+%` is the main result.** It is the increase in the median call of a
  stream when the 2 streams run together. The comparison is with the same
  threads when the other stream does not run.
- **The sweep ratio is the other main result.** For example, the line `one
  sweep of 1000 blocks: 649 ms mixed, 394 ms reads alone, 207 ms writes alone
  (sweep 3.13x slower mixed)` shows how much longer a full rewrite took under
  read load.
- **`wall ops/s` divides the calls of each stream by the same window.** So the
  2 stream rates add up to the total row.
- **The total row fills only the columns that add up.** A percentile across 2
  different operations does not describe a real operation. So those cells show
  `-`.
- **`calls` and `share %` show the real mix.** The split rounds the counts: 2
  workers give 1 reader and 1 writer, and 6 workers give 4 readers and 2
  writers. Use the measured share, not the nominal ratio.
- **The conflict count is 0 in a correct run.** Each writer owns a separate
  range. So a count that is not 0 means that the ranges are wrong, or that a
  process outside the run writes to the store. For conflict behavior, use
  `--group contention`.

### `in_memory` cannot show this

- **`InMemoryBackend` is pure Python. So its writer can hold the GIL for all
  of a short sweep,** and the readers then make no calls. 3 runs at
  `--blocks 120` gave 3634, 3472, and then 0 reads.
- **`sqlite` and the server targets release the GIL in the call.** So their
  readers always get time to run. The same 3 runs on `sqlite` gave 721, 723,
  and 722 reads.
- **Use the mixed numbers from `sqlite` or from a server target.** The `calls`
  column shows the problem. If a stream has only a small number of samples,
  its `+%` has no meaning. The p50 of one sample is only the duration of that
  one call.
- **The cause is the same as for the absent `in_memory` conflicts.** The
  section "The switch interval decides what a thread benchmark can see" above
  explains it.

## Targets

| Target | Storage | Needs |
| --- | --- | --- |
| `in_memory` | `InMemoryBackend`, one process, no persistent data | nothing |
| `sqlite` | `SQLiteBackend` on a file in a temporary directory, `durability="full"` | nothing |
| `postgres` | `PostgresBackend`, schema `genie_benchmark`, `durability="full"` | `postgres` extra, `GENIE_POSTGRES_DSN` |
| `mongodb` | `MongoDBBackend`, database `genie_benchmark`, `durability="full"` | `mongodb` extra, `GENIE_MONGODB_URI` |
| `neo4j` | `Neo4jBackend`, database `neo4j` | `neo4j` extra, `GENIE_NEO4J_URI` |
| `redis` | `RedisBackend`, key prefix `genie-benchmark` | `redis` extra, `GENIE_REDIS_URL` |

- **The 2 local targets always run.** `InMemoryBackend` needs nothing.
  `SQLiteBackend` uses the driver that comes with Python.
- **The 4 server targets read the same `GENIE_*` variables as the live adapter
  tests.** So one environment works for both. `GENIE_POSTGRES_SCHEMA`,
  `GENIE_MONGODB_DATABASE`, `GENIE_NEO4J_DATABASE`, and
  `GENIE_REDIS_KEY_PREFIX` override the namespace. The Neo4j credentials are
  `GENIE_NEO4J_USER` and `GENIE_NEO4J_PASSWORD`. The Redis credentials are
  `GENIE_REDIS_USER` and `GENIE_REDIS_PASSWORD`. If you set only one variable
  of a pair, the harness skips the target.
- **The report shows a skipped target as skipped.** It never counts it as a
  pass. The reason tells exactly what is not available: the extra to install,
  the variable to set, or both.
- **The namespace defaults are different from the test defaults on purpose.**
  A variant empties its store before it writes. `genie_benchmark` and
  `genie-benchmark` prevent the deletion of data that the live tests use.
  Neo4j is the exception: the benchmarks and the live tests both use the
  database `neo4j` by default. Use a target only with a store whose data you
  can lose.

### Running against servers

```bash
./lib/scripts/benchmark_full.sh
./lib/scripts/benchmark_full.sh --iterations 50 --blocks 500
./lib/scripts/benchmark_full.sh --group read --json reports/servers.json
./lib/scripts/benchmark_full.sh --group contention --workers 4 --blocks 200
```

- **`benchmark_full.sh` needs Docker.** It starts temporary PostgreSQL, Neo4j,
  MongoDB, and Redis containers. Then it runs the benchmarks and removes the
  containers.
- **It does not repeat the container setup.** `test_full.sh` already does that
  work. So `benchmark_full.sh` sets `GENIE_TEST_COMMAND` and then runs
  `test_full.sh`. The benchmark containers have their own names and ports, so
  a benchmark run and a test run can run at the same time.
- **The container start alone takes approximately one minute** before the
  first measurement. The script prints each step while it waits.
- **The run stops the driver notices for each query.** Neo4j sends a
  notification each time a query names a label, property, or relationship type
  that the database does not have yet. Almost all queries on a store that a
  variant just emptied do this. The runner sets the `neo4j.notifications`
  logger to `ERROR`. So a real driver failure still goes to the stream, but the
  notices do not.

### Why a server run takes so long

**Most of the time goes to corpus writes, not to measurement.** A run with
`--iterations 20 --blocks 200` used 82.7 s of its 103.8 s for setup:

| Target | Variants | Setup | Measurement |
| --- | --- | --- | --- |
| `in_memory` | 29 | 0.1s | 0.1s |
| `sqlite` | 29 | 0.9s | 0.7s |
| `postgres` | 29 | 15.5s | 5.2s |
| `mongodb` | 29 | 18.7s | 5.6s |
| `neo4j` | 29 | 37.6s | 7.3s |
| `redis` | 29 | 9.8s | 2.2s |

- **The cause is the method, not the adapters.** Each variant gets a new store
  and a new corpus, so no variant can change the numbers of the next variant.
  Thus each of the 29 variants writes `--blocks` + `--sections` + 2 blocks,
  with one round trip for each block. At the default settings, this is
  approximately 30,000 writes for each server target. `neo4j` uses 5.1 ms for
  each write.
- **So a default run on 6 targets takes 8 to 10 minutes.** With the smaller
  settings above, it takes less than 2 minutes. `--blocks` has a much larger
  effect on this time than `--iterations`, because it sets the size of the
  corpus that each variant writes.
- **A quick run during development:**

      ./lib/scripts/benchmark_full.sh --iterations 20 --warmup 5 --blocks 200 \
          --sections 5 --links-per-block 10

- **`--workers` adds time to both columns.** Each extra level repeats each
  selected variant, and each repeat writes a new corpus. A higher level does
  not make a larger corpus, but each of its workers makes its own
  `--iterations` calls. So a higher level adds measurement time, not setup
  time. Only a server run can show a speedup above 1 for the pooled adapters,
  so the extra time has value.
- **Use the default settings for a report that you will compare with later
  reports.** Compare only runs that have the same `settings` line, with the
  same worker levels.
- **The harness cannot discard a server store as it discards a temporary
  file.** So each variant empties the store when it opens it, and again when
  it closes it. The reset uses only `search` and `delete_block`. A delete of a
  root also deletes its subtree, and the links, key bindings, sequence entries,
  and temporal entries that the adapter owns for those blocks. Thus the reset
  works on each adapter without access to adapter internals. A new adapter
  needs no reset code of its own.

## Cases

| Group | Case | `backend` call | `memory` call |
| --- | --- | --- | --- |
| `write` | `put_root` | `put_block` with no parent | `Memory.put` |
| `write` | `put_child` | `put_block` under a section | `Memory.put(parent=...)` |
| `write` | `set_value` | `update_block(expected_version=...)` | `Memory.set` |
| `write` | `merge_value` | — | `Memory.update(value=...)` |
| `write` | `delete_leaf` | `delete_block` on a block with no children | `Memory.delete` |
| `write` | `delete_subtree` | `delete_block` on a subtree root | `Memory.delete` |
| `read` | `get` | `get_block` | `Memory.get` |
| `read` | `handle_get` | — | `MemoryHandle.get` |
| `read` | `children` | `children` | `Memory.children` |
| `read` | `ancestors` | `ancestors` | `Memory.ancestors` |
| `read` | `descendants` | `descendants` | — |
| `read` | `contains` | `contains` | `Memory.contains` |
| `link` | `add_link` | `add_link` | `Memory.add_link` |
| `link` | `list_links` | `list_links` | `Memory.list_links` |
| `search` | `search_subtree_text` | `search` at `Scope.SUBTREE` | `Memory.search` with the same query |
| `search` | `search_global_filter` | `search` at `Scope.GLOBAL` | `Memory.search` with the same query |
| `contention` | `hot_block_get` | `get_block` on one shared block | `Memory.get` on the same block |
| `contention` | `hot_parent_put` | `put_block` under one shared parent | `Memory.put(parent=...)` under it |
| `contention` | `hot_block_set` | — | `Memory.set` on one shared block |
| `contention` | `hot_block_merge` | — | `Memory.update(value=...)` on one shared block |
| `overhead` | `codec_round_trip` | — | `CodecRegistry.encode` then `decode` |
| `overhead` | `policy_check` | — | `PolicyEngine.allows` for `Operation.READ` |
| `overhead` | `hook_dispatch` | — | One `HookRegistry.operation` context with `before_put` and `after_put`, and no registered hooks |

3 details of the cases change how to read a row:

- **Each `backend` variant is exactly one protocol call.** `Memory.set` also
  does one `get_block` before its write. `Memory.delete` first reads the block
  and its subtree. So a part of their gap is extra store work, not Genie
  overhead. The `get` row shows the cost of that read, so you can subtract it.
- **`merge_value` and `descendants` have one layer on purpose.**
  `Memory.update` is a read, a decode, a merge, and a write. So no single
  backend call is equivalent. `descendants` is a `TraversalIndex` method, and
  `Memory` has no public wrapper for it.
- **`handle_get` has no `backend` variant,** because it is the `memory` layer
  with one more layer on top. Compare it with the `get` row of the same target.
  The difference is the cost that a bounded `MemoryHandle` adds to an unbounded
  `Memory`.

The `contention` group has 3 rules of its own:

- **It runs only if `--workers` gives a level above 1.** A shared target with
  one caller is not shared. If you use `--group contention` without such a
  level, the run stops with a message that names the necessary flag. If the
  run measures concurrency, these cases run at each level, also at level 1,
  because level 1 is the denominator of `speedup`.
- **Compare each case with the spread case that it mirrors:** `hot_block_get`
  with `get`, `hot_parent_put` with `put_root`, `hot_block_set` with
  `set_value`, and `hot_block_merge` with `merge_value`. Those spread cases
  give each worker its own ids and its own offset into the corpus. So the
  difference is contention, not store cost.
- **`hot_block_set` and `hot_block_merge` have only the `memory` layer,** for
  the same reason as `merge_value`. `Memory.set` reads and then does a
  compare-and-swap write in one call. `update_block` alone has no expected
  version until a separate `get_block` gives it. `hot_block_merge` also
  decodes and merges. So its time between the read and the write is longer,
  and it probably loses more races than `hot_block_set` on the same target.

## Flags

| Flag | Meaning |
| --- | --- |
| `--iterations` | Measured calls for each variant |
| `--warmup` | Calls that the runner makes and discards before the measurement starts |
| `--blocks` | Leaf blocks in the corpus of each variant |
| `--sections` | Blocks in the chain between the corpus root and its leaves |
| `--value-size` | Padding characters that the generator adds to each value |
| `--links-per-block` | Outgoing links on the corpus hub block |
| `--search-limit` | Result limit for search |
| `--seed` | Seed for the corpus generator |
| `--workers` | Callers that run one case at the same time. You can repeat this flag. The run always measures level 1 |
| `--workload` | Measure the mixed read/write scenario instead of the cases. Needs a `--workers` level above 1. A level of 4 or more gives the 3 to 1 split |
| `--layer` | Measure only this layer. You can repeat this flag |
| `--target` | Measure only this target. You can repeat this flag |
| `--group` | Measure only this group. You can repeat this flag |
| `--case` | Measure the cases whose name contains this text. You can repeat this flag |
| `--json` | Also write the full report to this path |
| `--list` | Print the targets and cases, then stop |
| `--quiet` | Write no progress lines and no tables |

`--iterations` and `--blocks` are the 2 settings that change the duration of a
run. `--workers` multiplies the effect of both: each level repeats each
selected variant, and each worker makes its own `--iterations` calls.

## The corpus

Each variant uses a corpus that the harness builds from the same seeded specs.

- **One root,** with the hub block and the section chain under it.
- **A chain of `--sections` blocks.** Each block is a child of the block
  before it. The chain sets the tree depth, so `ancestors` and `contains` do
  real work.
- **`--blocks` leaves, spread round-robin over the sections.** So each section
  has a similar number of children, and `children` does real work.
- **A hub block.** The `list_links` case adds `--links-per-block` outgoing
  links to it, and then reads them.
- **Text search looks for `gamma`.** This topic word is on approximately 1
  leaf in 5. The metadata filter matches `metadata.bucket == 3`, on
  approximately 1 leaf in 10.

## Metrics

Each variant reports these values:

- **`p50_ms`, `p95_ms`, `p99_ms`, `min_ms`, `max_ms`, `mean_ms`** — the call
  latency in milliseconds, from `time.perf_counter_ns`.
- **`ops_per_second`** — the rate that one caller sees, from the duration of
  its own calls. The value does not include setup and warmup.
- **`throughput_per_second`** — the rate of all callers together, from the
  wall time of the measured window. It counts only the calls that returned.
- **`workers` and `wall_seconds`** — the callers that ran at the same time,
  and the wall time of their measured window. `wall_seconds` is only in the
  JSON file. The concurrency and workload tables also show `workers`.
- **`samples`** — the measured calls that returned without an exception, for
  all workers together. The case tables do not show it, so they stay narrow.
  The workload table shows it as `calls`.
- **`errors` and `first_error`** — the calls that raised an unexpected
  exception, and the type and message of the first failure.
- **`conflicts`** — the calls that lost a race that the case expects to lose,
  for example a `MemoryConflict` on a block that another worker wrote first.
  The harness never adds them to `errors`, and they never cause a failed run.
- **`setup_seconds`** — the wall time to open the store, write the corpus, and
  prepare the targets that the variant uses for itself. No latency number
  includes it. Only in the JSON file.
- **`notes`** — the corpus scale, and what the calls did, for example the
  number of children that a traversal returned or the number of results that
  a search returned.

Each case with results at both store layers also reports **`overhead_ms`** and
**`overhead_percent`**. These values give the difference between the 2
`p50_ms` values.

Each worker level above 1 also reports **`speedup`**. This is its
`throughput_per_second` divided by the `throughput_per_second` of a single
caller. It is `null` if the single-caller rate is not available or is 0,
because then no correct divisor exists.

## Methodology

These decisions make 2 reports comparable. A change to one of them changes the
numbers.

- **Each variant gets a new store and a new corpus.** A variant can create,
  change, and delete blocks without limits, because its actions cannot change
  the measurement of the next variant. The case order never changes a number.
  The cost is setup time. `setup_seconds` reports it, and `--blocks` controls
  it.
- **The corpus comes from a seeded generator.** So the `backend` variant and
  the `memory` variant of one case see identical blocks, but each variant
  writes its own copy. The ids, types, values, parents, metadata, owner,
  policy, and version are the same. This makes the gap correct. Link ids are
  the only exception: `Memory.add_link` makes its own ids, so only the link
  count is the same.
- **The runner makes the warmup calls and discards their results.** So the
  first-call cost of a connection, a cache, or a query plan stays out of the
  percentiles.
- **Percentiles use the nearest-rank method.** So each reported value is a
  real measurement, not a value interpolated between 2 samples.
- **The runner counts a failed call, but does not time it.** It discards the
  duration, so one exception cannot look like a fast call.
- **The case builds the payload objects during setup.** Each call gets a
  payload that already exists. So the timed call measures the Genie operation,
  not the construction of the payload.
- **One principal owns all blocks.** So the default `owner_private` policy of
  each block allows all operations, and no case measures a denial path.
- **Garbage collection runs one time before the loop and stays on.** A caller
  in production also pays that cost.
- **The report records the environment:** the Python build, the operating
  system, the machine, the CPU count, the thread switch interval, the `genie`
  version, and the source commit. Latency changes with all of them.

With more than one worker, 4 more decisions apply:

- **Setup runs in the calling thread, for one worker at a time.** So
  preparation can never race, and the runner never times it. Worker `0` does
  the setup that the workers share, for example the block that a `contention`
  case competes for, or the hub links that `list_links` reads. So more workers
  never make a larger corpus.
- **Each worker gets its own ids and its own start offset.** A write case puts
  ids that contain the worker index. A rotation over the corpus starts at a
  different position for each worker. Without this, 4 workers would read the
  same ids in the same sequence, and a spread case would measure a hot case
  without a warning.
- **A barrier starts the measured window for all workers at the same
  instant.** Warmup occurs before the barrier, so a slow first call cannot
  occur in the measurement of a different worker. The wall time starts at that
  barrier and ends at the last return of the last worker.
- **The calling thread polls the progress. The workers never push it.** Each
  worker writes its count of completed calls into its own slot. The calling
  thread adds those slots. So the measured loop of a worker takes no lock, and
  the progress report cannot change the throughput.

## Reading the report

- **Compare a case with the same case in a different release,** not with a
  different case.
- **Compare targets only on the same case and the same settings.** The
  `settings` line shows those settings.
- **`search_limit` is the `limit` of each search.** The `backend` layer
  returns at most this number of candidates. The `memory` layer counts only
  the results that the principal can see. If hidden blocks match, it calls the
  backend again, so it can do more backend work than the `backend` layer.
  Each search case records `returned` together with `limit`.
- **`in_memory` is not always the fastest target.** It is the reference
  implementation, and its code is written for clarity. So an operation can
  cost more there than on a persistent adapter. An unexpected number is a
  result, not a harness bug. But examine it. If a row is much slower than
  similar rows, a real defect is as probable as a special property of the
  reference implementation.
- **Read a percentage together with the absolute numbers, never alone.** The
  `contains` row is the clearest example. The overhead table shows much more
  than 1000%, but `+ms` is approximately 0.02 ms. The backend call is so fast
  that all of the gap is the fixed `Memory` envelope. A large percentage of a
  very small number is not a problem.
- **A large gap almost always means extra round trips, not slow code.** This
  is the most useful result of the suite, and the server targets make it
  clear. `Memory` reads again the blocks that the backend already returned. So
  the cost increases with the number of results, and does not stay constant:
  - **`Memory.list_links`** returns a link only if the principal can read both
    endpoints. So it reads 2 blocks for each link. With 10 links on
    `postgres`, this is 20 extra point reads, and the row increases from
    0.89 ms to 17.24 ms.
  - **`Memory.search`** reads each result again. It evaluates the policy on
    the stored state, not on the indexed copy. At `Scope.SUBTREE`, it also
    does a containment check for each result. `Memory` filters visibility
    before and after the `after_search` hooks. The 2 filter passes share one
    verdict for each block id, so each result costs one read for all of the
    call. On `sqlite`, `search_subtree_text` costs 42.79 ms, and the raw call
    costs 32.39 ms.
  - **The cost is still one read for each result,** so the search cost
    increases with the page size. To remove this cost, the backend contract
    needs a batch read. The contract does not have one now.
  - **The pattern is the same on all server targets.** It is not an adapter
    problem. The cost increases with the number of returned results, not with
    the store.
  - **The second read is intentional.** A search index is derived state, and
    it can be stale. So `Memory` checks the policy against the stored block.
    The numbers raise a question about the number of round trips, not about
    the check itself.
- **A small negative overhead is noise, not a speedup.** The `memory` layer
  cannot be faster than the raw call that it wraps. A value a few percent
  below 0 means that the 2 variants ran at different times, and the state of
  the machine changed between them. This occurs most on the slowest targets.
  A value within a few percent of 0 means "no measurable difference".
- **A speedup near 1 is the expected result for an adapter that serializes,**
  not a problem. `SQLiteBackend` and `PostgresBackend` hold one lock around
  all of a transaction on one connection. So extra callers wait in a queue by
  design. There, the useful number is the effect of the queue on `p95 ms`.
- **0 conflicts on `in_memory` does not mean that the write was safe.** A
  pure-Python call is much shorter than the 5 ms thread switch interval. So 2
  workers almost never run between the read and the write of one pair. The
  same case on `sqlite` gets conflicts, because the driver releases the GIL in
  the call. `switch_interval_ms` in the report header explains this.
- **Compare `core` with the overhead table.** The 3 `core` helpers add up to
  approximately 0.013 ms, and the `get` overhead is approximately 0.015 ms.
  This agreement is a good sign. The gap on a simple read is the codec, the
  policy check, and the hook envelope, and nothing more. If a gap is much
  larger than that minimum, look for extra calls.
