# Repository Organization

Where every file in the Genie repository lives, and the rule that decides where
a new one goes.

## Top-level layout

Every file and folder in the repository, with what it holds. Tool caches, build
output, coverage reports, and local editor or agent files are ignored by
`.gitignore` and are not part of the layout.

```text
Genie/
├── .claudeignore       # paths Claude Code should not index (caches, builds, .git)
├── .gitignore          # ignored paths: caches, coverage, reports, local agent files
├── CONTRIBUTING.md     # setup, the checks to run, and where a new file belongs
├── design/             # these design notes, numbered `NN-topic.md`
├── docs/               # long-form documentation for readers of the library
├── examples/           # runnable examples, lamps, and their captured output
├── lib/                # the library itself, plus its tests, benchmarks, and scripts
├── LICENSE             # license text, named by `license-files` in `pyproject.toml`
├── pyproject.toml      # `genie` package metadata, extras, and build settings
├── pytest.ini          # repo-wide pytest settings: `testpaths` and `pythonpath`
├── README.md           # what Genie is, how to install it, and a first example
├── requirements.txt    # a note only: the core has no third-party runtime dependencies
├── ruff.toml           # repo-wide ruff settings: line length 193 and first-party roots
└── SECURITY.md         # how to report a vulnerability, and what is in scope
```

- **Four trees carry content:** `lib/`, `examples/`, `docs/`, and `design/`. Everything else at the root is configuration or project text.
- **`LICENSE` is empty today,** but `pyproject.toml` already points at it with `license-files = ["LICENSE"]`, so the file must stay where it is and needs its text before a release.
- **`requirements.txt` declares nothing.** It exists to answer the question it usually answers: the core has no runtime dependencies, and `pytest` and `ruff` come from the `dev` extra in `pyproject.toml`.

## `lib/` — the library

```text
lib/
├── src/genie/          # the installed package; the only code that ships
├── tests/              # core library tests
├── benchmarks/         # latency benchmark harness, standard library only
└── scripts/            # repository-wide developer scripts
```

- **`lib/` has no `README.md` and no config of its own.** It is a container, and its four children have separate jobs.
- **Only `lib/src/genie/` ships.** `[tool.setuptools.packages.find] where = ["lib/src"]` in `pyproject.toml` is what makes that true. Tests, benchmarks, and scripts stay outside the package.

### `lib/src/genie/` — the package

```text
lib/src/genie/
├── __init__.py                 # public API; every public name is re-exported here
├── py.typed                    # marks the package as typed for downstream checkers
├── memory/                     # blocks, links, handles, the Memory entry point, errors
│   ├── memory.py               # `Memory`: policy, hooks, codecs, and operation flow
│   ├── block.py                # `MemoryBlock` model
│   ├── links.py                # typed relationships between blocks
│   ├── handle.py               # `MemoryHandle`: root, principal, and capability limits
│   └── errors.py               # exceptions raised by memory operations
├── policy/                     # who may act on a block
│   ├── policy.py               # declarative access rules, including `owner_private()`
│   ├── engine.py               # policy evaluation for memory operations
│   └── principal.py            # principal identities used for decisions
├── query/                      # portable search vocabulary
│   ├── query.py                # `MemoryQuery`, shared by APIs and adapters
│   ├── filters.py              # filter normalization and matching helpers
│   ├── result.py               # search result model
│   └── scope.py                # supported search scopes
├── hooks/                      # lifecycle observation and customization
│   ├── base.py                 # the hook interface
│   ├── events.py               # typed context and event payloads
│   └── registry.py             # dispatch, causal context, suppression, reentrancy guards
├── codecs/                     # value translation at the storage boundary
│   ├── base.py                 # codec interfaces
│   ├── json.py                 # JSON-compatible default codec
│   └── stdlib.py               # opt-in codecs for common standard-library values
├── structures/                 # optional facades over the same blocks
│   ├── tree.py                 # `OwnershipTree`
│   ├── graph.py                # `PropertyGraph`
│   ├── keyed.py                # `KeyedCollection`
│   ├── log.py                  # `EventLog`
│   ├── temporal.py             # `TemporalCollection`
│   ├── lineage.py              # `LineageDAG`, composed from `PropertyGraph`
│   ├── versioned.py            # `VersionedRecords`, composed from keyed ids and a log
│   ├── access.py               # the memory access protocols facades depend on
│   ├── records.py              # immutable records shared by the facades
│   ├── _hooks.py               # internal hook integration (private)
│   └── _routing.py             # internal store resolution (private)
└── backend/                    # storage contracts and the shipped adapters
    ├── base.py                 # `MemoryBackend`, the top-level contract
    ├── capabilities.py         # the smaller capability protocols facades depend on
    ├── structure_capabilities.py  # optional DAG, keyed, ordered, and temporal protocols
    ├── in_memory/reference.py  # `InMemoryBackend`, the reference implementation
    ├── relational/sqlite.py    # `SQLiteBackend`, no extra package needed
    ├── relational/postgres.py  # `PostgresBackend`, `postgres` extra
    ├── graph/neo4j.py          # `Neo4jBackend`, `neo4j` extra
    ├── document/mongodb.py     # `MongoDBBackend`, `mongodb` extra
    ├── key_value/redis.py      # `RedisBackend`, `redis` extra
    └── composite/routed.py     # `RoutedBackend`, application-defined routes
```

- **Every subpackage has its own `README.md`,** including each storage family folder under `backend/`, and `[tool.setuptools.package-data]` ships them with the wheel, so the guide travels with the code.
- **Adapters are grouped by storage family,** not by vendor. A new adapter joins the family it belongs to, or starts a new folder when none fits.
- **A leading underscore means private.** `_hooks.py` and `_routing.py` are internal to `structures/` and are not re-exported.
- **`__init__.py` is the whole public surface.** A new public name must be added to the root `__all__`, and `lib/tests/test_public_imports.py` fails when it is missing.

### `lib/tests/` — core tests

```text
lib/tests/
├── test_memory_crud.py         # core CRUD, versions, traversal, recursive deletion
├── test_memory_handle.py       # bounded handles and restricted access
├── test_policy.py              # principals, roles, and block policies
├── test_links.py               # typed links and linked search
├── test_query_filters.py       # queries and portable filter behavior
├── test_hooks.py               # hook dispatch and customization
├── test_codecs.py              # codecs and the codec registry
├── test_public_imports.py      # every public name is importable from both surfaces
├── test_examples.py            # runs every example and diffs its output
├── test_benchmarks.py          # tests for the harness in `lib/benchmarks/`
├── backends/                   # adapter tests plus the reusable contract tests
│   ├── test_backend_contracts.py  # the shared suite every adapter must pass
│   ├── test_inmemory_backend.py
│   ├── test_sqlite_backend.py
│   ├── test_postgres_backend.py
│   ├── test_neo4j_backend.py
│   ├── test_mongodb_backend.py
│   ├── test_redis_backend.py
│   └── test_routed_backend.py
└── structures/                 # one file per facade, plus cross-cutting suites
    ├── test_tree.py                   # `OwnershipTree`
    ├── test_graph.py                  # `PropertyGraph`
    ├── test_keyed.py                  # `KeyedCollection`
    ├── test_log.py                    # `EventLog`
    ├── test_temporal.py               # `TemporalCollection`
    ├── test_lineage.py                # `LineageDAG`
    ├── test_versioned.py              # `VersionedRecords`
    ├── test_records.py                # the shared immutable records
    ├── test_access.py                 # shared access protocol behavior
    ├── test_composition.py            # several facades over the same blocks
    ├── test_structure_policy.py       # facades keep policy and handle checks
    └── test_structure_concurrency.py  # atomic behavior under concurrent callers
```

- **`test_backend_contracts.py` is the reusable suite.** A new adapter, shipped or application-owned, is expected to run it rather than write its own version.
- **Live adapter tests skip themselves** when the optional driver is missing or the `GENIE_*` connection variables are unset. A plain `pytest` run is therefore partial, and `lib/scripts/test_full.sh` is the complete run.

### `lib/benchmarks/` and `lib/scripts/`

```text
lib/benchmarks/
├── run.py              # command line entry point
├── runner.py           # runs the cases and returns a report
├── cases.py            # the operations being measured
├── workload.py         # the mixed read/write scenario and its worker roles
├── dataset.py          # seeded corpus every variant runs against
├── targets.py          # the stores being measured
├── settings.py         # run settings shared by every case
├── metrics.py          # latency statistics for one variant
├── environment.py      # machine and source facts recorded with each report
├── progress.py         # live progress while a run is in flight
├── report.py           # readable table or JSON output
└── README.md           # how to run and read a benchmark

lib/scripts/
├── test_full.sh                # full suite against disposable containers (needs Docker)
├── benchmark_full.sh           # the benchmarks against those same containers
├── capture_example_output.py   # refresh `examples/expected/`
└── ruff_report.sh              # regenerate `ruff_report.txt`
```

- **Benchmarks stay out of `lib/tests/`** so a plain `pytest` run never times operations. Their own tests live in `lib/tests/test_benchmarks.py`.
- **The harness uses the standard library only,** like the core package.
- **`benchmark_full.sh` reuses `test_full.sh`.** The container setup exists once, and `GENIE_TEST_COMMAND` chooses what runs against it.

## `examples/` — runnable code

```text
examples/
├── basic/              # 17 single-file examples, one topic each
├── advanced/           # 7 single-file examples that combine several ideas
├── lamps/              # 5 package-sized architectural demonstrations
├── expected/           # captured stdout: basic/, advanced/, lamps/
├── _bootstrap.py       # puts `lib/src` and the root on `sys.path` before install
├── _catalog.py         # discovers the examples and where their output lives
└── README.md           # reading order and how to run everything
```

- **Everything runs from the repository root.** Single-file examples run as scripts (`python examples/basic/crud_blocks.py`); lamps are packages and run as modules (`python -m examples.lamps.mem0.demo`).
- **Every example is output-tested.** `lib/tests/test_examples.py` runs each one and compares the whole standard output against `examples/expected/`. Refresh it with `lib/scripts/capture_example_output.py` when the output changes on purpose.
- **`_catalog.py` finds the examples from the layout:** every `*.py` in `basic/` and `advanced/`, and each lamp's `demo.py`. The capture script and the test both read it, so the two cannot drift apart.
- **A new example must also join `RUNNABLE_EXAMPLES`** in `lib/tests/test_examples.py`. That list is explicit, and a test asserts it matches what `_catalog.py` discovers.
- **`basic/` is one idea per file,** `advanced/` combines several. Split rather than combine when an example starts covering two topics.

### A lamp

```text
examples/lamps/<name>/
├── __init__.py         # the lamp's public names
├── demo.py             # the entry point, run as a module
├── agent.py            # the deterministic agent that drives the demo
├── model.py            # the lamp's own value types
├── structures.py       # which Genie facades the lamp composes
├── <domain>.py         # the lamp's own logic, named after its concepts
├── README.md           # concept mapping to the upstream project, and its limits
└── tests/              # that lamp's tests, kept with the lamp
```

- **Lamps are architectural demonstrations,** not compatibility layers and not production implementations. Each keeps the main memory idea of the project it represents, plus one typical workflow.
- **Their tests stay with them,** under `examples/lamps/<name>/tests`, not in `lib/tests/`.
- **Taxonomy, retrieval, and agent behavior live in the lamp,** which is exactly the point: it shows the boundary the core library holds.

## `docs/` and `design/`

- **`docs/design.md`** is the reader-facing architecture document: model, safety, hooks, structures, and backends. Keep it in sync when the public memory model, architecture, or safety boundaries change.
- **Package `README.md` files** hold the detail for one area, and `docs/design.md` links to them instead of repeating them.
- **`design/` holds these notes,** numbered `NN-topic.md`. They are working documents about intent and organization, written for the people building Genie rather than for its users.

## Why every config file sits at the root

| File | Reason it cannot move into `lib/` |
| --- | --- |
| `pyproject.toml` | `readme` and `license-files` resolve next to it and cannot point outside its directory, and `pip install` from a git URL only looks at the repository root |
| `pytest.ini` | It covers `examples/` as well as `lib/`, and sets `pythonpath` to `lib/src`, `lib`, and `.` |
| `ruff.toml` | Ruff finds settings by walking up from each file, so a config inside `lib/` would never reach `examples/` |

- **Run everything from the repository root:** `python -m pip install -e .`, `python -m pytest`, `python -m ruff check .`.
- **`pyproject.toml` still points at the real source** with `[tool.setuptools.packages.find] where = ["lib/src"]`, so the root config and the nested layout agree.

## Where a new file goes

| You are adding | It goes in |
| --- | --- |
| Core library code | `lib/src/genie/<area>/` |
| A public name | the area module, plus the root `__all__` in `lib/src/genie/__init__.py` |
| A backend adapter | `lib/src/genie/backend/<family>/` |
| A core test | `lib/tests/` |
| An adapter or contract test | `lib/tests/backends/` |
| A structure facade test | `lib/tests/structures/` |
| A benchmark case | `lib/benchmarks/`, with its test in `lib/tests/test_benchmarks.py` |
| A developer script | `lib/scripts/` |
| A small example | `examples/basic/` or `examples/advanced/`, listed in `RUNNABLE_EXAMPLES` in `lib/tests/test_examples.py`, with output in `examples/expected/` |
| A lamp | `examples/lamps/<name>/`, with tests in `examples/lamps/<name>/tests/` |
| Architecture documentation | `docs/design.md`, or the `README.md` of the area |
| A design note | `design/NN-topic.md` |

## Related reading

- [Overview](01-overview.md) — the model these directories implement
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, the checks to run, and the boundaries a change should respect
- [Architecture](../docs/design.md) — the public memory model in detail
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — reading order and how to run them
