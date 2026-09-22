# Testing Strategy

What Genie's tests prove, how the suite is split into layers, and which checks
run for each kind of change.

## At a glance

| Layer | Where | What it proves | Needs |
| --- | --- | --- | --- |
| Core behavior | `lib/tests/test_*.py` | `Memory`, handles, policy, links, queries, hooks, codecs | Nothing |
| Public surface | `lib/tests/test_public_imports.py` | Every public name imports from the root and from its subpackage | Nothing |
| Structure facades | `lib/tests/structures/` | Each facade alone, together over the same blocks, under policy, and under concurrent callers | Nothing |
| Backend contracts | `lib/tests/backends/test_backend_contracts.py` | One shared suite of capability rules, run against every adapter | Depends on the adapter |
| Adapter tests, offline | `lib/tests/backends/test_<adapter>_backend.py` | Configuration checks, error mapping, serialization, construction probes, all with stand-in drivers | Nothing |
| Adapter tests, driver only | Same files | How the adapter uses the real client library | The adapter's extra |
| Adapter tests, live | Same files | The contract suite and adapter-specific behavior on a real server | The extra and the `GENIE_*` variables |
| Examples | `lib/tests/test_examples.py` | Every example runs from the repository root and prints its captured output | Nothing |
| Lamps | `examples/lamps/<name>/tests/` | Each lamp's concept mapping and its documented limits | Nothing |
| Benchmark harness | `lib/tests/test_benchmarks.py` | The harness still works, with tiny settings | Nothing |

- **A plain `pytest` run covers every row except the live one** and the
  driver-only tests of any extra that is not installed. Report it as partial.
- **`lib/scripts/test_full.sh` covers every row.** It is the complete run.

## What the tests prove

The suite is built around Genie's guarantees, not around its files. Each
guarantee has a place where it is tested.

| Guarantee | Main tests |
| --- | --- |
| **Scoped access cannot be widened.** A child handle cannot escape its parent, change its principal, or widen global or ancestor access | `test_memory_handle.py`, `test_structure_policy.py` |
| **Policy is checked on every read path.** `children`, `ancestors`, search, and hook-rewritten results are all filtered | `test_policy.py`, `test_hooks.py` |
| **Hooks cannot bypass authorization.** A `before_*` hook cannot change a parent, a policy, or a query after it was authorized | `test_hooks.py` |
| **Writes do not silently lose changes.** A stale `expected_version` raises `MemoryConflict` | `BlockStoreContract`, `test_sqlite_backend.py` |
| **Atomic operations are atomic.** Only one of many concurrent writers can claim a key, append a position, or close a cycle | The `*_is_atomic` contract tests, `test_structure_concurrency.py` |
| **Recursive delete is all or nothing.** A subtree changed by another writer is not half deleted | `BlockStoreContract`, adapter tests |
| **Stored values round-trip.** Values and metadata come back unchanged, and corrupt stored rows are reported, not guessed | Adapter tests, `test_codecs.py` |
| **Missing drivers fail clearly.** A missing driver raises `UnsupportedOperation` that names the extra | Adapter tests, offline |
| **Examples stay true.** Printed output matches `examples/expected/` | `test_examples.py` |

## Backend contract suites

- **One suite per capability protocol.** `test_backend_contracts.py` defines
  `BlockStoreContract`, `TraversalIndexContract`, `KeyedStoreContract`,
  `SequenceStoreContract`, `TemporalStoreContract`, `TemporalIndexContract`,
  `LinkStoreContract`, and `AcyclicLinkStoreContract`.
- **An adapter opts in by subclassing.** A test class inherits the contracts for
  the capabilities the adapter claims and implements `make_backend()`. It does
  not copy the tests.
- **A capability the adapter does not claim is not tested against it.** The
  suites follow the capability protocols, so no adapter is expected to
  implement every one. `RoutedBackend` has a test that checks it claims no
  structure capability of its own.
- **The contracts check relationships, not strategy.** `TraversalIndexContract`
  compares `children` and `descendants` as sets, because their order is
  backend-defined, and checks order only for `ancestors`, where the protocol
  promises nearest-parent-first. How an adapter answers is its own choice.
- **The reference and local backends always run the suites.**
  `TestInMemoryStructureBackendContracts` and
  `TestSQLiteStructureBackendContracts` need no server, so every plain run
  checks the contracts.
- **Server adapters run the same suites live.** Each live subclass cleans its
  data in `setup_method` and again in `teardown_method`, so tests do not see
  each other's blocks.
- **An application-owned adapter can reuse the suites** too, instead of
  writing its own version of the rules.

## Concurrency tests

- **Concurrency is tested by forcing a real race.** A test starts many
  threads with a `ThreadPoolExecutor`, holds them at a `threading.Barrier`, and
  releases them together.
- **The assertion is about the outcome, not the timing.** Exactly one caller
  succeeds, the others raise `MemoryConflict`, and the stored state holds
  exactly one result.
- **Capability atomicity lives in the contracts,** for example
  `test_keyed_store_bind_is_atomic` and
  `test_acyclic_link_check_and_insert_is_atomic`. Every adapter that claims the
  capability runs it.
- **Races that cross facades live in `test_structure_concurrency.py`,** for
  example a `PropertyGraph` edge and a `LineageDAG` edge that together would
  form a cycle on one relation.
- **Races across separate connections or processes** are tested per adapter,
  for example `test_sqlite_backend_coordinates_separate_connections` and
  `test_live_postgres_backend_persists_and_coordinates_instances`.

## Adapter tests in three tiers

| Tier | Gate | Typical subjects |
| --- | --- | --- |
| **Offline** | None | Invalid configuration rejected before the driver loads; the missing-driver message; strict JSON serialization; server error codes mapped to Genie errors; construction probes, such as the replica role check |
| **Driver only** | The driver is importable | How the adapter configures the real client, for example the Redis retry options |
| **Live** | The driver is importable and the `GENIE_*` variables are set | The contract suites; persistence across reconnects; byte-order text comparison; the public `Memory` API on the real store |

- **Offline tests use stand-ins, not mocks of the whole driver.** Small
  `_Fake<Thing>` classes and probe connections return exactly the answer a
  test needs, such as a replica role or a dropped connection.
- **Construction probes are tested on both sides.** A proven bad answer
  refuses, an unreadable answer lets construction pass, and a connection
  failure is not hidden.
- **The live gate is two checks in one `skipif`:** `importlib.util.find_spec`
  finds the driver, and the connection variable is set. The skip reason names
  both, so a skipped run says how to enable it.
- **Live tests use their own namespace.** `GENIE_POSTGRES_SCHEMA`,
  `GENIE_MONGODB_DATABASE`, and `GENIE_REDIS_KEY_PREFIX` default to test names,
  and the tests delete everything in that namespace.
- **Only the images in `test_full.sh` are tested.** The image list and the
  connection variables are in [Software stack](05-software-stack.md).

## Examples and lamps as tests

- **Every example is a test.** `test_example_matches_its_captured_output` runs
  each script as a subprocess from the repository root and compares the whole
  standard output with its file in `examples/expected/`.
- **The list cannot drift.** `test_every_example_script_has_smoke_coverage`
  checks that `RUNNABLE_EXAMPLES` matches what `examples/_catalog.py`
  discovers, and `test_no_orphan_captured_output` finds captured output whose
  example is gone.
- **A failure says how to fix it.** The message names
  `lib/scripts/capture_example_output.py` for an intended output change.
- **Assertions inside an example state its guarantee.** The printed lines are
  what is compared; the `assert` lines are what the example proves.
- **Lamp tests check behavior, not the upstream library.** They cover the
  lamp's concept mapping and its documented limits, with no model calls and no
  third-party imports.

## Determinism

- **These are rules, from `CONTRIBUTING.md`:**
  - **No dependence on the clock.** Tests pass explicit timezone-aware
    `datetime` values, such as `datetime(2026, 1, 1, tzinfo=UTC)`.
  - **No network** outside the gated live tests.
  - **No dependence on the machine.** A test does not change its result because
    a server happens to be configured or a package happens to be installed.
    That is why every optional dependency is behind a gate.
- **Fixed ids where the value matters.** Contract helpers such as `put_blocks`
  and `put_tree` create blocks with given ids, so failures are easy to read.
- **Files go in `tmp_path`.** SQLite tests that need a real file use the
  `pytest` temporary directory. Others use `:memory:`.
- **Benchmarks never run inside `pytest`.** Timing is not deterministic, so
  `lib/benchmarks/` is outside `testpaths`, and `test_benchmarks.py` checks only
  that the harness works.

## Which run goes with a change

| Change touches | Run |
| --- | --- |
| Anything | `python -m pytest`, `python -m ruff check .`, `python -m compileall -q lib/src/genie lib/tests examples` |
| An adapter, the backend contracts, or concurrency behavior | Also `./lib/scripts/test_full.sh` |
| An example's output, on purpose | Also `python lib/scripts/capture_example_output.py`, then review the diff in `examples/expected/` |

- **A pull request lists the runs behind it.** A plain `pytest` run is marked
  as partial when a change could affect an adapter.

## Where a new test goes

| Change | Test to add or update |
| --- | --- |
| **Core behavior or a public API** | A function in the matching `lib/tests/test_*.py` |
| **A new public name** | Nothing new; `test_public_imports.py` already fails until the name is in the root `__all__` |
| **A new or changed backend capability** | A contract class in `test_backend_contracts.py`, subclassed by every adapter that claims it |
| **An atomic operation** | An `*_is_atomic` contract test with a `Barrier` race |
| **A new shipped adapter** | `test_<adapter>_backend.py` with offline tests, a gated live contract subclass, and a container in `test_full.sh` |
| **A structure facade** | Its file in `lib/tests/structures/`, plus `test_structure_policy.py` or `test_composition.py` when it combines with other facades |
| **A new example** | An entry in `RUNNABLE_EXAMPLES` and its captured output |
| **A lamp** | `examples/lamps/<name>/tests/` |
| **A bug fix** | A regression test that fails without the fix |

- **Test style,** such as plain functions, inline setup, and sentence-style
  names, is in [Coding style](08-coding-style.md).

## Not tested on purpose

| Left out | Why |
| --- | --- |
| **Timing and throughput** | Belongs to `lib/benchmarks/`, which is not a pass or fail check |
| **Server versions outside the `test_full.sh` images** | No run stands behind them; the adapter README states the server requirements |
| **Scale-out deployments,** such as Redis Cluster | Out of scope for the shipped adapters |
| **Agent behavior and model output** | Genie stores values. Deciding what they mean belongs to the application |
| **Type checking** | No type checker is configured; types are kept in review |
| **CI** | No CI configuration is in the repository; the checks run locally |

## Related reading

- [Software stack](05-software-stack.md) — test servers, images, ports, and `GENIE_*` variables
- [Repository organization](07-repo-organization.md) — where each test file lives
- [Coding style](08-coding-style.md) — how tests are written
- [Detailed architecture](04-detailed-architecture.md) — the invariants the tests protect
- [Backends](../lib/src/genie/backend/README.md) — adapter behavior and deployment responsibilities
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup and the checks to run
