# Benchmarks

Latency for Genie's raw memory operations, one store at a time, at two API
layers, with one caller or with several.

- **Question it answers:** what does one operation cost, how much of that cost
  is Genie itself rather than the store underneath it, and what changes when
  more than one caller works at the same time.
- **Why it exists:** to hold baseline numbers a release can be compared
  against, so a slowdown is visible before users find it.
- **Not a test suite.** Nothing asserts. `pytest` does not collect this
  directory, so a normal test run stays fast. `lib/tests/test_benchmarks.py`
  keeps the harness itself working.
- **Not a comparison with other memory libraries.** Those call a model before
  they write, so their write latency measures a model provider and not
  memory-system code.
- **No extra packages.** The harness uses the standard library only, the same
  rule the core package follows.

## Run

From the repository root:

```bash
conda run -n "genie" python lib/benchmarks/run.py
conda run -n "genie" python lib/benchmarks/run.py --list
conda run -n "genie" python lib/benchmarks/run.py --iterations 500 --blocks 5000
conda run -n "genie" python lib/benchmarks/run.py --target sqlite --group read
conda run -n "genie" python lib/benchmarks/run.py --layer backend --layer memory
conda run -n "genie" python lib/benchmarks/run.py --workers 4 --workers 8
conda run -n "genie" python lib/benchmarks/run.py --json reports/latest.json
```

- **A default run takes about thirty seconds** and needs no server and no
  extra package.
- **Exit status is `1` when any measured call raised,** and `0` otherwise.
  Timing is never a pass or fail signal, because a shared machine is too noisy
  for that, and a conflict a `contention` case expected is not an error.
- **`--workers` is the only concurrency switch.** Without it the run measures
  one caller and produces exactly the tables it always did.
- **`--quiet` drops the progress lines and the tables,** which is what you want
  together with `--json`.

## The three layers

Every case declares the layers it can be measured at. This split is the point
of the suite.

| Layer | What it times |
| --- | --- |
| `backend` | One call on a backend capability protocol. No policy check, no hook dispatch, no codec, no block copying |
| `memory` | The matching call on the public `Memory` or `MemoryHandle` API, which is what an application actually pays |
| `core` | One core-library helper on its own, with no store behind it |

- **The gap between `backend` and `memory` is the overhead table.** It says
  what Genie adds on top of the store for the same logical operation.
- **The `core` cases explain that gap.** They price the codec, the
  `PolicyEngine`, and the hook envelope separately, so a large gap can be
  attributed instead of guessed at.
- **`core` runs once, in its own section.** A core helper has no store behind
  it, so repeating it under every target would only duplicate the same number.
  `--target` does not affect it; `--layer core` and `--group overhead` do.


## Concurrency

`--workers` measures the same case with several callers at once. It is off by
default, so a plain run keeps its length and its numbers.

```bash
conda run -n "genie" python lib/benchmarks/run.py --workers 4 --workers 8
conda run -n "genie" python lib/benchmarks/run.py --group contention --workers 4
```

- **Workers is a third axis** next to the target and the layer. Any case can be
  measured at any level, so `search` under eight callers is as available as
  `hot_block_set` is.
- **Every worker shares one `Memory` and one backend,** which is the shape an
  application has. They are threads, not processes.
- **`--iterations` and `--warmup` count per worker.** Four workers make four
  times as many calls. Latency stays comparable across levels while throughput
  grows.
- **Level 1 is always measured,** because `speedup` divides by it. Asking for
  `--workers 8` measures 1 and 8.
- **`core` cases stay at level 1.** A core helper has no store behind it, so
  there is nothing to contend on.
- **The levels of one case run back to back,** so the runs being compared
  happen close together and a drifting machine cannot move `speedup` on its
  own.

### The two rates

| Number | Question it answers |
| --- | --- |
| `ops_per_second` | What one caller saw, from the time its own calls took |
| `throughput_per_second` | What every caller together achieved, from the wall time of the measured window |
| `speedup` | `throughput_per_second` divided by the single caller's |

- **A speedup near 1 means the store serializes.** More callers are not
  finishing more work, they are queueing for the same turn.
- **A speedup below 1 is normal for an adapter that serializes,** because the
  callers now pay for lock handoff on top of the same serialized work.
- **Latency and throughput move in opposite directions here.** A queued caller
  waits longer, so `p50_ms` and especially `p95_ms` grow while the store
  finishes about the same number of calls per second. Read both columns.

### Conflicts

- **A conflict is a lost race, not a failure.** `Memory.set` and
  `Memory.update` read a block and then write it with `expected_version`. When
  another worker wrote first, the call raises `MemoryConflict`.
- **Conflicts are counted apart from errors,** in their own column, and never
  change the exit status. A case says which exception types it expects to lose
  to; anything else is still an error.
- **A conflicted call is not timed.** It did not finish its work, so keeping it
  out of the percentiles is the honest choice, and
  `throughput_per_second` counts only the calls that committed.
- **Genie does not retry.** The conflict reaches the caller, which is the
  contract. These cases measure how often an application would have to.

### What each adapter can show

The `speedup` column is readable only next to the adapter's concurrency model.

| Adapter | In-process model | What a concurrent run shows |
| --- | --- | --- |
| `InMemoryBackend` | One `RLock` taken per operation | Calls queue, so speedup stays near 1 |
| `SQLiteBackend` | One connection, one `RLock` held for a whole read or write transaction | Every operation serializes |
| `PostgresBackend` | Same, plus `pg_advisory_xact_lock` so writes serialize across processes too | Every operation serializes |
| `MongoDBBackend` | Pooled client; writes take a database-resident guard document | Reads overlap, writes queue |
| `Neo4jBackend` | Pooled driver; writes take a database-resident guard | Reads overlap, writes queue |
| `RedisBackend` | Pooled client | Reads overlap |

- **These are not defects.** The shipped adapters target one small, controlled
  database, and a single guarded writer is what makes their compound "read,
  check, then write" steps atomic.
- **A serializing adapter still reports useful numbers.** Latency under load
  and the tail say how long a caller waits for its turn, which is what an
  application feels.

### The switch interval decides what a thread benchmark can see

- **CPython runs one thread at a time** and switches only every
  `sys.getswitchinterval()` seconds, 5 ms by default. Every report records the
  value as `switch_interval_ms`, because it moves the conflict counts more than
  anything else in the environment.
- **A pure-Python call is far shorter than 5 ms,** so one worker finishes
  thousands of `InMemoryBackend` operations inside a single turn. Its read and
  its write are almost never split by another worker.
- **So `hot_block_set` on `in_memory` reports zero conflicts,** even at eight
  workers and thousands of writes to one block.
- **That is a fact about this interpreter, not a safe backend.** The same eight
  workers writing the same block 2000 times each produce over twelve thousand
  conflicts once `sys.setswitchinterval` is lowered to a microsecond.
- **Any adapter that talks to a server releases the GIL inside the call,** so
  its workers interleave for real and its conflict counts mean what they say.
  `sqlite` conflicts on a hot block. `in_memory` does not.
- **Read `in_memory` concurrency numbers as a floor,** and take conflict
  behavior from a server target.

## Targets

| Target | Storage | Needs |
| --- | --- | --- |
| `in_memory` | `InMemoryBackend`, one process, nothing persisted | nothing |
| `sqlite` | `SQLiteBackend` on a file in a throwaway directory, `durability="full"` | nothing |
| `postgres` | `PostgresBackend`, schema `genie_benchmark`, `durability="full"` | `postgres` extra, `GENIE_POSTGRES_DSN` |
| `mongodb` | `MongoDBBackend`, database `genie_benchmark`, `durability="full"` | `mongodb` extra, `GENIE_MONGODB_URI` |
| `neo4j` | `Neo4jBackend`, database `neo4j` | `neo4j` extra, `GENIE_NEO4J_URI` |
| `redis` | `RedisBackend`, key prefix `genie-benchmark` | `redis` extra, `GENIE_REDIS_URL` |

- **The two local targets always run.** `InMemoryBackend` needs nothing and
  `SQLiteBackend` uses the driver that ships with Python.
- **The four server targets read the same `GENIE_*` variables the live adapter
  tests read,** so one environment serves both. `GENIE_POSTGRES_SCHEMA`,
  `GENIE_MONGODB_DATABASE`, `GENIE_NEO4J_DATABASE`, and
  `GENIE_REDIS_KEY_PREFIX` override the namespace. Neo4j and Redis credentials
  are `GENIE_NEO4J_USER` / `GENIE_NEO4J_PASSWORD` and `GENIE_REDIS_USER` /
  `GENIE_REDIS_PASSWORD`, and each pair must be set together or not at all.
- **A skipped target is reported as skipped,** never folded into a pass, and
  the reason names exactly what is missing: which extra to install, which
  variable to set, or both.
- **The namespace defaults are deliberately not the test ones.** A variant
  empties its store before it writes, so `genie_benchmark` and
  `genie-benchmark` keep a benchmark run from clearing data the live tests are
  using. Point a target at a store you do not mind losing.

### Running against servers

```bash
./lib/scripts/benchmark_full.sh
./lib/scripts/benchmark_full.sh --iterations 50 --blocks 500
./lib/scripts/benchmark_full.sh --group read --json reports/servers.json
./lib/scripts/benchmark_full.sh --group contention --workers 4 --blocks 200
```

- **It needs Docker,** and it starts throwaway PostgreSQL, Neo4j, MongoDB, and
  Redis containers, runs the benchmarks, and disposes of them.
- **It does not repeat the container setup.** `test_full.sh` already does that
  work, so `benchmark_full.sh` sets `GENIE_TEST_COMMAND` and hands over. The
  containers use their own names and ports, so a benchmark run and a test run
  can be in flight together.
- **Container startup alone is about a minute** before the first measurement,
  and the script prints each step while it waits.
- **Per-query driver notices are silenced for the run.** Neo4j reports a
  notification whenever a query names a label, property, or relationship type
  the database has not stored yet, which is nearly every query against a store
  a variant has just emptied. The runner raises those loggers to `ERROR`, so a
  real driver failure still reaches the stream while the notices do not.

### Why a server run takes so long

**Writing corpora, not measuring.** A run at `--iterations 20 --blocks 200`
spent 82.7s of its 103.8s on setup:

| Target | Variants | Setup | Measuring |
| --- | --- | --- | --- |
| `in_memory` | 29 | 0.1s | 0.1s |
| `sqlite` | 29 | 0.9s | 0.7s |
| `postgres` | 29 | 15.5s | 5.2s |
| `mongodb` | 29 | 18.7s | 5.6s |
| `neo4j` | 29 | 37.6s | 7.3s |
| `redis` | 29 | 9.8s | 2.2s |

- **The cause is the methodology, not the adapters.** Every variant gets a
  fresh store and a fresh corpus, so no variant can move the next one's
  numbers. That means 29 variants each write `--blocks` + 2 blocks, one round
  trip per block. At the default `--blocks 1000` that is about 29,000 writes
  per server target, and `neo4j` pays 5.1 ms for each one.
- **So budget eight to ten minutes for a default six-target run,** against
  under two minutes for the reduced settings above. `--blocks` moves this far
  more than `--iterations`, because it sets the corpus every variant rewrites.
- **A quick pass while you work:**

      ./lib/scripts/benchmark_full.sh --iterations 20 --warmup 5 --blocks 200 \
          --sections 5 --links-per-block 10

- **`--workers` multiplies the measuring column, not the setup one.** Each
  extra level repeats every selected variant, and the corpus is written once
  per variant whatever the level. A server run is the only place the pooled
  adapters can show a speedup above 1, so it is worth the time.
- **Keep the defaults for a report you intend to compare against later,** and
  only compare runs whose `settings` line matches, worker levels included.
- **A server store cannot be thrown away like a temporary file,** so each
  variant empties it on open instead. The reset uses only `search` and
  `delete_block`: deleting a root cascades to its subtree and to the links,
  key bindings, sequence entries, and temporal entries the adapter owns for
  those blocks. That is why it works on any adapter without reaching into one,
  and why a new adapter needs no reset code of its own.

## Cases

| Group | Case | `backend` call | `memory` call |
| --- | --- | --- | --- |
| `write` | `put_root` | `put_block` with no parent | `Memory.put` |
| `write` | `put_child` | `put_block` under a section | `Memory.put(parent=...)` |
| `write` | `set_value` | `update_block(expected_version=...)` | `Memory.set` |
| `write` | `merge_value` | — | `Memory.update(value=...)` |
| `write` | `delete_leaf` | `delete_block` on a childless block | `Memory.delete` |
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
| `overhead` | `hook_dispatch` | — | One `HookRegistry.operation` context with no hooks registered |

Three case-level details change how a row should be read:

- **Every `backend` variant is exactly one protocol call.** `Memory.set` also
  performs one `get_block` before its write, and `Memory.delete` reads the
  subtree first, so part of their gap is extra store work rather than Genie
  overhead. The `get` row prices that read, so it can be subtracted.
- **`merge_value` and `descendants` are single-layer on purpose.**
  `Memory.update` is a read, a decode, a merge, and a write, so it has no
  single backend equivalent. `descendants` is a `TraversalIndex` method with no
  public `Memory` wrapper.
- **`handle_get` has no `backend` variant** because it is the `memory` layer
  twice over. Read it against the `get` row of the same target: the difference
  is what a bounded `MemoryHandle` adds over an unbounded `Memory`.

The `contention` group has three rules of its own:

- **It runs only when `--workers` names a level above 1.** A shared target with
  one caller is not shared. Asking for `--group contention` without a level is
  refused with a message that names the missing flag. When concurrency is
  measured they run at every level, level 1 included, because that is the
  denominator of `speedup`.
- **Read each one against the spread case it mirrors.** `hot_block_get` against
  `get`, `hot_parent_put` against `put_root`, `hot_block_set` against
  `set_value`. Those cases give every worker its own ids and its own offset
  into the corpus, so the difference is contention and not store cost.
- **`hot_block_set` and `hot_block_merge` are `memory` only,** for the same
  reason `merge_value` is: `Memory.set` reads and then compare-and-swap writes
  inside one call, and `update_block` alone has no version to expect until a
  separate `get_block` supplies it. `hot_block_merge` decodes and merges as
  well, so its window between the read and the write is wider and it should
  lose more races than `hot_block_set` on the same target.

## Flags

| Flag | Meaning |
| --- | --- |
| `--iterations` | Measured calls per variant |
| `--warmup` | Calls made and thrown away before measuring starts |
| `--blocks` | Leaf blocks in the corpus each variant runs against |
| `--sections` | Blocks in the chain between the corpus root and its leaves |
| `--value-size` | Padding characters added to each generated value |
| `--links-per-block` | Outgoing links attached to the corpus hub block |
| `--search-limit` | Candidate budget passed to search |
| `--seed` | Seed for the corpus generator |
| `--workers` | Callers running one case at the same time. Repeatable. Level 1 is always measured |
| `--layer` | Measure one layer only. Repeatable |
| `--target` | Measure one target only. Repeatable |
| `--group` | Measure one group only. Repeatable |
| `--case` | Measure cases whose name contains this text. Repeatable |
| `--json` | Also write the full report to this path |
| `--list` | Print the targets and cases, then exit |
| `--quiet` | Write no progress lines and no tables |

`--iterations` and `--blocks` are the two dials that change how long a run
takes, and `--workers` multiplies both: each level repeats every selected
variant, and each worker makes its own `--iterations` calls.

## The corpus

Each variant runs against a corpus built from the same seeded specs.

- **One root,** with the hub block and the section chain under it.
- **A chain of `--sections` blocks,** each a child of the one before it. The
  chain sets tree depth, so `ancestors` and `contains` have real work to do.
- **`--blocks` leaves spread round-robin over the sections,** so every section
  has a similar number of children and `children` has real work to do.
- **A hub block** carrying `--links-per-block` outgoing links, which is what
  `list_links` reads.
- **Text search looks for `gamma`,** a topic word on roughly one leaf in five.
  The metadata filter matches `metadata.bucket == 3`, roughly one leaf in ten.

## Metrics

Every variant reports:

- **`p50_ms`, `p95_ms`, `p99_ms`, `min_ms`, `max_ms`, `mean_ms`** — call
  latency in milliseconds, from `time.perf_counter_ns`.
- **`ops_per_second`** — the rate one caller sees, from the time its own calls
  took. Setup and warmup are excluded.
- **`throughput_per_second`** — the rate every caller together achieved, from
  the wall time of the measured window. Only calls that returned are counted.
- **`workers` and `wall_seconds`** — the callers that ran at the same time, and
  the wall time of their measured window. In the JSON only.
- **`samples`** — measured calls that returned without raising, every worker's
  merged. In the JSON only, to keep the tables narrow.
- **`errors` and `first_error`** — calls that raised something unexpected, and
  the first failure's type and message.
- **`conflicts`** — calls that lost a race the case expected to lose, such as a
  `MemoryConflict` on a block another worker wrote first. Never folded into
  `errors`, and never a reason to fail a run.
- **`setup_seconds`** — wall time spent writing the corpus and the variant's
  own targets, which is excluded from every latency number. In the JSON only.
- **`notes`** — the corpus scale, plus what the calls actually did, such as how
  many children a traversal returned or how many results a search returned.

Each case measured at both store layers also reports **`overhead_ms`** and
**`overhead_percent`**, the difference between the two `p50_ms` values.

Each worker level above 1 also reports **`speedup`**, its
`throughput_per_second` divided by the single caller's. It is `null` when the
single-caller rate is missing or zero, because there is then nothing honest to
divide by.

## Methodology

These choices are what make two reports comparable. Changing one changes the
numbers.

- **Every variant gets a fresh store and a fresh corpus.** A variant may
  create, change, and delete blocks freely, because nothing it does can change
  what the next variant measures. Case order never moves a number. The cost is
  setup time, which `setup_seconds` reports and `--blocks` controls.
- **The corpus comes from a seeded generator,** so the `backend` variant and
  the `memory` variant of one case see identical blocks even though each writes
  its own copy. Same ids, types, values, parents, metadata, owner, policy, and
  version. That is what makes the gap honest. Link ids are the one exception:
  `Memory.add_link` assigns its own, so only the link count matches.
- **Warmup calls are made and thrown away,** so the first-call cost of a
  connection, a cache, or a query plan stays out of the percentiles.
- **Percentiles use the nearest-rank method,** so every reported value is a
  real measurement and not a point interpolated between two samples.
- **A failed call is counted, not timed.** Its duration is dropped, so one
  exception cannot be mistaken for a fast call.
- **Payload objects are built once per variant** and passed to every call, so
  the timed call covers the Genie operation and not payload construction.
- **One principal owns every block,** so each block's default `owner_private`
  policy allows every operation and no case measures a denial path.
- **Garbage collection runs once before the loop and stays enabled,** because a
  caller in production pays that cost too.
- **The report records the environment** — Python build, operating system,
  machine, CPU count, thread switch interval, `genie` version, and source
  commit — because latency moves with all of them.

At more than one worker, four more choices apply:

- **Setup runs in the calling thread, one worker at a time.** Preparation can
  therefore never race and is never timed. Worker `0` does the setup the
  workers share, such as creating the block a `contention` case fights over or
  the hub links `list_links` walks, so more workers never mean more corpus.
- **Every worker gets its own ids and its own starting offset.** A write case
  puts ids carrying the worker index, and a rotation over the corpus starts at
  a different place per worker. Without that, four workers would walk in
  lockstep and a spread case would quietly measure a hot one.
- **A barrier starts the measured window for every worker at the same instant.**
  Warmup happens before it, so a slow first call cannot land inside another
  worker's measurement. Wall time runs from that barrier to the last worker's
  last return.
- **Progress is polled, never pushed.** Each worker writes its own finished-call
  count into its own slot and the calling thread sums those slots, so no lock
  enters a worker's hot path and reporting cannot distort throughput.

## Reading the report

- **Compare a case with itself across releases,** not with a different case.
- **Compare targets only on the same case and the same settings.** The
  `settings` line says what those were.
- **`search_limit` is a candidate budget, not a result count.** Backends apply
  it before policy and scope filtering, so a search can return fewer blocks
  than the budget. Each search case records `returned` next to
  `candidate_budget` for exactly this reason.
- **`in_memory` is not automatically the fastest target.** It is the reference
  implementation, written for clarity, so an operation can cost more there than
  on a persistent adapter. A surprising number is a result, not a harness bug.
  Chase it anyway: a row far slower than its siblings is as likely to be a real
  defect as a quirk of the reference.
- **Read a percentage next to the absolute numbers, never on its own.** The
  `contains` row is the clearest example: the overhead table reports well over
  1000% while `+ms` is about 0.02 ms. The backend call became so cheap that
  the whole gap is the fixed `Memory` envelope. A large share of a tiny number
  is not a problem.
- **A large gap almost always means extra round trips, not slow code.** This is
  the most useful thing the suite reports, and the server targets make it
  obvious. `Memory` re-reads blocks the backend has already returned, so the
  cost grows with the number of results rather than staying flat:
  - **`Memory.list_links`** returns a link only when the principal can read
    both endpoints, so it reads two blocks per link. With ten links on
    `postgres` that is twenty extra point reads, and the row goes from 0.89 ms
    to 17.24 ms.
  - **`Memory.search`** re-fetches every result to evaluate policy against
    stored state rather than the indexed copy, plus a containment check per
    result at `Scope.SUBTREE`. Visibility is filtered both before and after the
    `after_search` hooks, and the two passes share one verdict per block id, so
    a result costs one read for the whole call. On `sqlite`,
    `search_subtree_text` costs 42.79 ms against 32.39 ms for the raw call.
  - **One read per result is still the shape,** so search cost grows with the
    page size. Removing it would need a batched read in the backend contract,
    which is not there today.
  - **The pattern is the same on every server target,** and it is not an
    adapter problem: the cost grows with the number of results returned, not
    with the store.
  - **The re-read is deliberate,** because a search index is derived state and
    may be stale, so policy has to be checked against the stored block. The
    question the numbers raise is the round-trip count, not whether to check.
- **A small negative overhead is noise, not a speedup.** The `memory` layer
  cannot be faster than the raw call it wraps. A few percent below zero means
  the two variants ran far enough apart for the machine to drift, which shows
  up most on the slowest targets. Treat anything inside a few percent of zero
  as "no measurable difference".
- **A speedup near 1 is the expected answer for most shipped adapters,** not a
  disappointment. `SQLiteBackend` and `PostgresBackend` hold one lock around a
  whole transaction on one connection, so extra callers queue by design. The
  useful number there is what the queue does to `p95 ms`.
- **Zero conflicts on `in_memory` does not mean the write was safe.** A
  pure-Python call is far shorter than the 5 ms thread switch interval, so two
  workers almost never split one read-then-write pair. The same case on
  `sqlite` conflicts, because the driver releases the GIL inside the call.
  `switch_interval_ms` in the report header is the number that explains it.
- **Read `core` against the overhead table.** The three `core` helpers add up
  to about 0.013 ms, and the `get` overhead is about 0.015 ms. That agreement
  is a good sign: the gap on a simple read is the codec, the policy check, and
  the hook envelope, and nothing hidden. Where a gap is much larger than that
  floor, look for extra calls.

## Not here yet

1. **Mixed read and write load.** Every worker of a variant makes the same call
   today, so one latency number describes the whole variant. Readers running
   while writers work would need a result pool per role and a second set of
   columns.