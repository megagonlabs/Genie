# Benchmarks

Latency for Genie's raw memory operations, one store at a time, at two API
layers.

- **Question it answers:** what does one operation cost, and how much of that
  cost is Genie itself rather than the store underneath it.
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
conda run -n "genie" python lib/benchmarks/run.py --json reports/latest.json
```

- **A default run takes about thirty seconds** and needs no server and no
  extra package.
- **Exit status is `1` when any measured call raised,** and `0` otherwise.
  Timing is never a pass or fail signal, because a shared machine is too noisy
  for that.
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

## Targets

| Target | Storage |
| --- | --- |
| `in_memory` | `InMemoryBackend`, one process, nothing persisted |
| `sqlite` | `SQLiteBackend` on a file in a throwaway directory, `durability="full"` |

- **Both always run.** `InMemoryBackend` needs nothing and `SQLiteBackend` uses
  the driver that ships with Python.
- **`skip_reason` is already part of the target shape,** so the server-backed
  adapters can report a reason instead of a number once they are added.
- **A skipped target is reported as skipped,** never folded into a pass.

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
| `--layer` | Measure one layer only. Repeatable |
| `--target` | Measure one target only. Repeatable |
| `--group` | Measure one group only. Repeatable |
| `--case` | Measure cases whose name contains this text. Repeatable |
| `--json` | Also write the full report to this path |
| `--width` | Characters to fit the tables into. `0` prints every column |
| `--list` | Print the targets and cases, then exit |
| `--quiet` | Write no progress lines and no tables |

`--iterations` and `--blocks` are the two dials that change how long a run
takes.

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
- **`samples`** — measured calls that returned without raising. In the JSON
  only, to keep the tables narrow.
- **`errors` and `first_error`** — calls that raised, and the first failure's
  type and message.
- **`setup_seconds`** — wall time spent writing the corpus and the variant's
  own targets, which is excluded from every latency number. In the JSON only.
- **`notes`** — the corpus scale, plus what the calls actually did, such as how
  many children a traversal returned or how many results a search returned.

Each case measured at both store layers also reports **`overhead_ms`** and
**`overhead_percent`**, the difference between the two `p50_ms` values.

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
  machine, CPU count, `genie` version, and source commit — because latency
  moves with all of them.

## Output

Two tables per section: a latency table with one row per measured variant, and
an overhead table with one row per case measured at both store layers.

- **The target is a section heading,** not a column. Repeating it on every row
  costs width and reads worse.
- **Numbers are right aligned** and names are left aligned, so decimal points
  line up.
- **Latency is printed with four decimals,** because the `core` helpers are
  sub-microsecond and three decimals of a millisecond would round them to zero.
- **A table too wide for the terminal drops a column,** `p99 ms` first and then
  `p95 ms`, and the overhead table gives up `+%` because its two `p50` columns
  already carry it. A closing line names everything left out.
- **`case`, `layer`, `p50 ms`, `ops/s`, and `errors` are never dropped.**
  Without them a row cannot be identified or read. That sets a floor: below it
  the table stays wider than the window.
- **A pipe or a file is never trimmed.** Only a terminal has a width to
  respect, so redirected output keeps every column. `--width` still applies if
  you ask for it.

Two escape hatches:

```bash
# Print every column, no trimming, however narrow the window
conda run -n "genie" python lib/benchmarks/run.py --width 0

# Fit a width you choose, such as a code block in a document
conda run -n "genie" python lib/benchmarks/run.py --width 72
```

## Progress

Progress goes to standard error while the run is in flight, so the tables and
the JSON file stay clean and a slow case is visibly working.

- **In a terminal, one line updates in place** with the variant number, the
  calls finished, the seconds spent, and a rough time remaining:

      [58/61] sqlite memory: search_subtree_text  60/110 calls  3.1s  about 3s left

- **In a pipe or a log file, whole lines are written far apart** — every five
  seconds instead of five times a second — because nothing there can erase a
  line and a fast update rate would only fill the log.
- **Each variant closes with what it measured,** so the scrollback keeps a
  record:

      [58/61] sqlite memory: search_subtree_text  100 calls in 5.8s  p50 52.290 ms

- **Progress is never written inside a timed call.** The reporter is told about
  a call only after its duration has been read, so a progress line cannot
  inflate a measurement.

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
  It is worth chasing, though: the first run of this suite found that
  `InMemoryBackend.contains` materialized the whole subtree, which made
  `contains` about 3900 times more expensive than it needed to be and dragged
  `handle_get` and `search_subtree_text` down with it. It now walks up from the
  candidate, like the MongoDB and Redis adapters.
- **Read a percentage next to the absolute numbers, never on its own.** The
  `contains` row is the clearest example: the overhead table reports well over
  1000% while `+ms` is about 0.02 ms. The backend call became so cheap that
  the whole gap is the fixed `Memory` envelope. A large share of a tiny number
  is not a problem.
- **A large gap usually means extra calls, not slow code.** The clearest case
  left in the report is **`list_links`**, which returns links only when the
  principal can read both endpoints, so `Memory.list_links` reads two blocks
  per link. At `--links-per-block 50` that is 100 extra point reads behind one
  call, and it is why the row costs about twelve times the raw call on
  `in_memory` and twenty-four times on `sqlite`.
- **Read `core` against the overhead table.** The three `core` helpers add up
  to about 0.013 ms, and the `get` overhead is about 0.015 ms. That agreement
  is a good sign: the gap on a simple read is the codec, the policy check, and
  the hook envelope, and nothing hidden. Where a gap is much larger than that
  floor, look for extra calls.

## Not here yet

Planned next, in order:

1. **Server-backed targets.** `PostgresBackend`, `MongoDBBackend`,
   `Neo4jBackend`, and `RedisBackend`, gated on the same `GENIE_*` variables
   the live tests already read, plus a script that starts throwaway containers
   the way `lib/scripts/test_full.sh` does.
2. **Concurrency and contention.** Worker levels, wall-clock throughput next to
   the single-caller rate, and hot-block cases.
3. **Structure facades.** One operation per facade family.
4. **Saved baselines** plus a comparison command for release checks.
