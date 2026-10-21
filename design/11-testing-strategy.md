# Testing Strategy

This note describes what the Genie tests prove, how the suite divides into
layers, and which checks run for each type of change.

## At a glance

| Layer | Where | What it proves | Needs |
| --- | --- | --- | --- |
| Core behavior | `lib/tests/test_*.py` | `Memory`, handles, policy, links, queries, hooks, codecs | Nothing |
| Public surface | `lib/tests/test_public_imports.py` | Each public name imports from the root package and from its subpackage | Nothing |
| Structure facades | `lib/tests/structures/` | Each facade alone, with other facades over the same blocks, under policy, and with concurrent callers | Nothing |
| Backend contracts | `lib/tests/backends/test_backend_contracts.py` | One shared suite of capability rules that runs against each adapter | Depends on the adapter |
| Adapter tests, offline | `lib/tests/backends/test_<adapter>_backend.py` | Configuration checks, the map from driver errors to Genie errors, serialization, and construction probes, all with stand-in drivers | Nothing |
| Adapter tests, driver only | Same files | How the adapter uses the real client library | The extra of the adapter |
| Adapter tests, live | Same files | The contract suite and adapter-specific behavior on a real server | The extra and the `GENIE_*` variables |
| Examples | `lib/tests/test_examples.py` | Each example runs from the repository root and prints its captured output | Nothing |
| Lamps | `examples/lamps/<name>/tests/` | The concept mapping of each lamp and its documented limits | Nothing |
| Benchmark harness | `lib/tests/test_benchmarks.py` | The harness works with very small settings | Nothing |

- **A plain `pytest` run covers all rows except the live row.** It also skips
  the driver-only tests of each extra that is not installed. So a plain run is a
  partial run.
- **`lib/scripts/test_full.sh` covers all rows.** It is the complete run.

## What the tests prove

The suite follows the guarantees of Genie, not its files. Each guarantee has a
place where a test checks it.

| Guarantee | Main tests |
| --- | --- |
| **Scoped access cannot become wider.** A child handle cannot go outside its parent, change its principal, or widen global or ancestor access | `test_memory_handle.py`, `test_structure_policy.py` |
| **Genie checks policy on each read path.** Genie filters `children`, `ancestors`, search results, and results that a hook rewrote | `test_policy.py`, `test_hooks.py` |
| **Hooks cannot bypass authorization.** A `before_*` hook cannot change a parent, a policy, or a query after Genie authorized it | `test_hooks.py` |
| **A write does not lose changes silently.** A stale `expected_version` raises `MemoryConflict` | `BlockStoreContract`, `test_sqlite_backend.py` |
| **Atomic operations are atomic.** Only one of many concurrent writers can claim a key, append a position, or close a cycle | The `*_is_atomic` contract tests, `test_structure_concurrency.py` |
| **Recursive delete is all or nothing.** If another writer changes the subtree, the delete does not remove only a part of it | `BlockStoreContract`, adapter tests |
| **One block id has one route.** `RoutedBackend` instances that share a `route_catalog` cannot store one id in 2 routes, and a stale route cache does not hide a block | `RouteCatalogContract`, `test_routed_backend.py` |
| **Stored values survive a round trip.** Values and metadata come back unchanged, with dict keys in the order of the write. The adapter reports corrupt stored rows and does not guess their content | `BlockStoreContract`, `LinkStoreContract`, adapter tests, `test_codecs.py` |
| **A driver that is not installed gives a clear error.** The adapter raises `UnsupportedOperation`, and the message names the extra | Adapter tests, offline |
| **Examples stay correct.** The printed output agrees with `examples/expected/` | `test_examples.py` |

## Backend contract suites

- **One suite for each capability protocol.** `test_backend_contracts.py`
  defines `BlockStoreContract`, `TraversalIndexContract`, `KeyedStoreContract`,
  `SequenceStoreContract`, `TemporalStoreContract`, `TemporalIndexContract`,
  `LinkStoreContract`, `AcyclicLinkStoreContract`, and `RouteCatalogContract`.
- **An adapter selects its suites through a subclass.** A test class inherits
  the contracts for the capabilities that the adapter claims. The class
  implements `make_backend()`. It does not copy the tests.
- **The suites do not test an adapter for a capability that it does not
  claim.** The suites follow the capability protocols, so they do not expect
  each adapter to implement all protocols. `RoutedBackend` routes links, so it
  runs `LinkStoreContract` and `AcyclicLinkStoreContract`, with and without a
  `route_catalog`. A separate test checks that it claims no record-store
  capability of its own.
- **The contracts check relationships, not strategy.** `TraversalIndexContract`
  compares `children` and `descendants` as sets, because the backend defines
  their order. It checks order only for `ancestors`, because there the protocol
  promises the nearest parent first. Each adapter chooses how it gets the
  answer.
- **The reference and local backends always run the suites.**
  `TestInMemoryStructureBackendContracts` and
  `TestSQLiteStructureBackendContracts` need no server, so each plain run
  checks the contracts.
- **Server adapters run the same suites live.** Each live subclass removes its
  data in `setup_method` and again in `teardown_method`. So a test does not see
  the blocks of another test.
- **An application-owned adapter can also use the suites.** Then the
  application does not write its own version of the rules.

## Concurrency tests

- **Concurrency tests cause a real race.** A test starts many threads with a
  `ThreadPoolExecutor` and holds them at a `threading.Barrier`. Then it
  releases them together.
- **The assertion is about the result, not the timing.** Exactly one caller
  succeeds, and the other callers raise `MemoryConflict`. The stored state holds
  exactly one result.
- **The contracts hold the capability atomicity tests,** for example
  `test_keyed_store_bind_is_atomic` and
  `test_acyclic_link_check_and_insert_is_atomic`. Each adapter that claims the
  capability runs them.
- **`test_structure_concurrency.py` holds the races that cross facades.** An
  example is a `PropertyGraph` edge and a `LineageDAG` edge that close a cycle
  on one relation if both succeed.
- **`test_routed_backend.py` holds the races between composite instances.**
  Several `RoutedBackend` instances that share a `route_catalog` put one id at
  the same time, each into a different route, and exactly one stores it.
- **Each adapter tests its own races across separate connections or
  processes,** for example `test_sqlite_backend_coordinates_separate_connections`
  and `test_live_postgres_backend_persists_and_coordinates_instances`.

## Adapter tests in three tiers

| Tier | Gate | Typical subjects |
| --- | --- | --- |
| **Offline** | None | Rejection of an invalid configuration before the driver loads, the message for a driver that is not installed, strict JSON serialization, the map from server error codes to Genie errors, and construction probes, for example the replica role check |
| **Driver only** | The driver is importable | How the adapter configures the real client, for example the Redis retry options |
| **Live** | The driver is importable and the `GENIE_*` variables are set | The contract suites, persistence across reconnects, byte-order text comparison, and the public `Memory` API on the real store |

- **Offline tests use stand-ins, not mocks of the full driver.** Small
  `_Fake<Thing>` classes and probe connections return exactly the answer that a
  test needs, for example a replica role or a dropped connection.
- **The tests check construction probes in both directions.** If the probe
  proves a bad answer, the constructor refuses. If the answer is unreadable,
  the constructor does not refuse. The constructor does not hide a connection
  failure.
- **The live gate is 2 checks in one `skipif`:** `importlib.util.find_spec`
  finds the driver, and the connection variable has a value. The skip reason
  names both checks, so a skipped run tells how to enable the tests.
- **Live tests use their own namespace.** The defaults of
  `GENIE_POSTGRES_SCHEMA`, `GENIE_MONGODB_DATABASE`, and
  `GENIE_REDIS_KEY_PREFIX` are test names. The tests delete all data in that
  namespace.
- **The tests run only against the images in `test_full.sh`.**
  [Software stack](05-software-stack.md) gives the image list and the
  connection variables.

## Examples and lamps as tests

- **Each example is a test.** `test_example_matches_its_captured_output` runs
  each script as a subprocess from the repository root. It compares all of the
  standard output with the file of that script in `examples/expected/`.
- **The list cannot become out of date.**
  `test_every_example_script_has_smoke_coverage` checks that
  `RUNNABLE_EXAMPLES` agrees with the examples that `examples/_catalog.py`
  finds. `test_no_orphan_captured_output` finds captured output for an example
  that does not exist.
- **A failure tells how to fix it.** For an intended output change, the message
  names `lib/scripts/capture_example_output.py`.
- **Assertions inside an example state its guarantee.** The test compares the
  printed lines. The `assert` lines are what the example proves.
- **Lamp tests check behavior, not the upstream library.** They cover the
  concept mapping of the lamp and its documented limits. They make no model
  calls and use no third-party imports.

## Determinism

- **These are rules, from `CONTRIBUTING.md`:**
  - **No dependence on the clock.** Tests give explicit timezone-aware
    `datetime` values, for example `datetime(2026, 1, 1, tzinfo=UTC)`.
  - **No network** outside the gated live tests.
  - **No dependence on the machine.** The result of a test does not change if
    a server is configured or if a package is installed. For this reason, each
    optional dependency is behind a gate.
- **Fixed ids if the id value is important.** Contract helpers, for example
  `put_blocks` and `put_tree`, create blocks with given ids. So failures are
  easy to read.
- **Files go in `tmp_path`.** If an SQLite test needs a real file, it uses the
  `pytest` temporary directory. Other SQLite tests use `:memory:`.
- **Benchmarks never run inside `pytest`.** Timing is not deterministic. So
  `lib/benchmarks/` is outside `testpaths`, and `test_benchmarks.py` checks only
  that the harness works.

## Which run goes with a change

| Change touches | Run |
| --- | --- |
| Anything | `python -m pytest`, `python -m ruff check .`, `python -m compileall -q lib/src/genie lib/tests examples` |
| An adapter, the backend contracts, or concurrency behavior | Also `./lib/scripts/test_full.sh` |
| The output of an example, on purpose | Also `python lib/scripts/capture_example_output.py`, then a review of the diff in `examples/expected/` |

- **A pull request lists the runs that support it.** If a change can affect an
  adapter, the pull request marks a plain `pytest` run as partial.

## Where a new test goes

| Change | Test to add or update |
| --- | --- |
| **Core behavior or a public API** | A function in the related `lib/tests/test_*.py` file |
| **A new public name** | No new test. `test_public_imports.py` fails until the name is also in the root `__all__` |
| **A new or changed backend capability** | A contract class in `test_backend_contracts.py`. Each adapter that claims the capability subclasses it |
| **An atomic operation** | An `*_is_atomic` contract test with a `Barrier` race |
| **A new shipped adapter** | `test_<adapter>_backend.py` with offline tests, a gated live contract subclass, and a container in `test_full.sh` |
| **A structure facade** | The test file of the facade in `lib/tests/structures/`. Also `test_structure_policy.py` or `test_composition.py` if the facade combines with other facades |
| **A new example** | An entry in `RUNNABLE_EXAMPLES` and its captured output |
| **A lamp** | `examples/lamps/<name>/tests/` |
| **A bug fix** | A regression test that fails without the fix |

- **Test style** is in [Coding style](08-coding-style.md), for example plain
  functions, inline setup, and sentence-style names.

## Not tested on purpose

| Left out | Why |
| --- | --- |
| **Timing and throughput** | `lib/benchmarks/` measures them. It is not a pass or fail check |
| **Server versions outside the `test_full.sh` images** | No test run covers them. The adapter README gives the server requirements |
| **Scale-out deployments,** for example Redis Cluster | Out of scope for the shipped adapters |
| **Agent behavior and model output** | Genie stores values. The application decides what they mean |
| **Type checking** | The repository configures no type checker. Code review checks the types |
| **CI** | The repository has no CI configuration. The checks run locally |

## Related reading

- [Software stack](05-software-stack.md) — test servers, images, ports, and `GENIE_*` variables
- [Repository organization](07-repo-organization.md) — the location of each test file
- [Coding style](08-coding-style.md) — the style of the tests
- [Detailed architecture](04-detailed-architecture.md) — the invariants that the tests protect
- [Backends](../lib/src/genie/backend/README.md) — adapter behavior and deployment responsibilities
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup and the checks to run
