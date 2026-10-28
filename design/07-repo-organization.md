# Repository Organization

This note shows where each file in the Genie repository is. It also gives the
rule that decides where a new file goes.

## Top-level layout

This tree shows each file and folder at the repository root, and what it holds.
`.gitignore` ignores tool caches, build output, coverage reports, and local
editor or agent files. They are not part of the layout.

```text
Genie/
├── .claudeignore       # paths that Claude Code does not index (caches, builds, .git)
├── .gitignore          # ignored paths: caches, coverage, reports, local agent files
├── CONTRIBUTING.md     # setup, the checks to run, and where a new file goes
├── design/             # these design notes, numbered `NN-topic.md`
├── examples/           # runnable examples, lamps, and their captured output
├── lib/                # the library itself, and its tests, benchmarks, and scripts
├── LICENSE             # license text, named by `license-files` in `pyproject.toml`
├── pyproject.toml      # `genie` package metadata, extras, and build settings
├── pyrightconfig.json  # repo-wide Pyright settings: checked folders, import paths, and Python 3.11
├── pytest.ini          # repo-wide pytest settings: `testpaths` and `pythonpath`
├── README.md           # what Genie is, how to install it, and a first example
├── requirements.txt    # a note only: the core has no third-party runtime dependencies
├── ruff.toml           # repo-wide ruff settings: line length 193 and first-party roots
└── SECURITY.md         # how to report a vulnerability, and what is in scope
```

- **The content is in 3 trees:** `lib/`, `examples/`, and `design/`. All other files at the root are configuration or project text.
- **`LICENSE` holds the BSD 3-Clause text** for the `1.0.0` release. `pyproject.toml` refers to it with `license-files = ["LICENSE"]`. So the file stays at the root.
- **`requirements.txt` declares nothing.** Its only purpose is to answer the usual question about dependencies. The core has no runtime dependencies, and `pyright`, `pytest`, and `ruff` come from the `dev` extra in `pyproject.toml`.

## `lib/` — the library

```text
lib/
├── src/genie/          # the installed package, and the only code in the wheel
├── tests/              # core library tests
├── benchmarks/         # latency benchmark harness, standard library only
└── scripts/            # repository-wide developer scripts
```

- **`lib/` has no `README.md` and no config of its own.** It is only a container. Its 4 children have separate purposes.
- **Only `lib/src/genie/` is in the package.** `[tool.setuptools.packages.find] where = ["lib/src"]` in `pyproject.toml` causes this. Tests, benchmarks, and scripts stay outside the package.

### `lib/src/genie/` — the package

```text
lib/src/genie/
├── __init__.py                 # public API, with a re-export of each public name
├── py.typed                    # marks the package as typed for type checkers in applications
├── memory/                     # blocks, links, handles, the Memory entry point, errors
│   ├── memory.py               # `Memory`: policy, hooks, codecs, and operation flow
│   ├── block.py                # `MemoryBlock` model
│   ├── links.py                # typed relationships between blocks
│   ├── handle.py               # `MemoryHandle`: root, principal, and capability limits
│   ├── errors.py               # exceptions that memory operations raise
│   └── _validation.py          # internal checks for ids, block types, and relations (private)
├── policy/                     # who can act on a block
│   ├── policy.py               # declarative access rules, for example `owner_private()`
│   ├── engine.py               # policy evaluation for memory operations
│   └── principal.py            # principal identities for access decisions
├── query/                      # portable search vocabulary
│   ├── query.py                # `MemoryQuery`, shared by APIs and adapters
│   ├── filters.py              # filter normalization and match helpers
│   ├── result.py               # search result and search cursor models
│   ├── paging.py               # search order and cursor paging for adapters
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
│   ├── access.py               # the memory access protocols that facades use
│   ├── records.py              # immutable records that the facades share
│   ├── _hooks.py               # internal hook integration (private)
│   └── _routing.py             # internal store resolution (private)
└── backend/                    # storage contracts and the shipped adapters
    ├── base.py                 # `MemoryBackend`, the top-level contract
    ├── capabilities.py         # the smaller capability protocols that facades use
    ├── structure_capabilities.py  # optional DAG, keyed, ordered, and temporal protocols
    ├── in_memory/reference.py  # `InMemoryBackend`, the reference implementation
    ├── relational/sqlite.py    # `SQLiteBackend`, needs no extra package
    ├── relational/postgres.py  # `PostgresBackend`, `postgres` extra
    ├── graph/neo4j.py          # `Neo4jBackend`, `neo4j` extra
    ├── document/mongodb.py     # `MongoDBBackend`, `mongodb` extra
    ├── key_value/redis.py      # `RedisBackend`, `redis` extra
    └── composite/routed.py     # `RoutedBackend`, application-defined routes
```

- **Each subpackage has its own `README.md`.** Each storage family folder under `backend/` also has one. `[tool.setuptools.package-data]` puts these files in the wheel, so the guide stays with the code.
- **The folders group adapters by storage family, not by vendor.** A new adapter goes into the folder of its family. If no family fits, the adapter gets a new folder.
- **A leading underscore means private.** `_validation.py` is internal to `memory/`, and `_hooks.py` and `_routing.py` are internal to `structures/`. The package does not re-export them.
- **`__init__.py` is the whole public surface.** The root `__all__` contains each public name. `lib/tests/test_public_imports.py` fails if a subpackage exports a name that the root `__all__` does not contain.

### `lib/tests/` — core tests

```text
lib/tests/
├── test_memory_crud.py         # core CRUD, versions, traversal, recursive delete
├── test_memory_handle.py       # bounded handles and restricted access
├── test_policy.py              # principals, roles, and block policies
├── test_links.py               # typed links and linked search
├── test_query_filters.py       # queries and portable filter behavior
├── test_hooks.py               # hook dispatch and customization
├── test_codecs.py              # codecs and the codec registry
├── test_public_imports.py      # each public name imports from both surfaces
├── test_examples.py            # runs each example and compares its output
├── test_benchmarks.py          # tests for the harness in `lib/benchmarks/`
├── backends/                   # adapter tests and the reusable contract tests
│   ├── test_backend_contracts.py  # the shared suite that runs against each adapter
│   ├── test_inmemory_backend.py
│   ├── test_sqlite_backend.py
│   ├── test_postgres_backend.py
│   ├── test_neo4j_backend.py
│   ├── test_mongodb_backend.py
│   ├── test_redis_backend.py
│   └── test_routed_backend.py
└── structures/                 # one file for each facade, and suites that cover several facades
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

- **`test_backend_contracts.py` is the reusable suite.** A new adapter, shipped or application-owned, runs this suite. It does not write its own version of the suite.
- **Live adapter tests skip themselves** if the optional driver is not installed or the `GENIE_*` connection variables are not set. So a plain `pytest` run is partial. `lib/scripts/test_full.sh` is the complete run.

### `lib/benchmarks/` and `lib/scripts/`

```text
lib/benchmarks/
├── run.py              # command line entry point
├── runner.py           # runs the cases and returns a report
├── cases.py            # the operations that the harness measures
├── workload.py         # the mixed read/write scenario and its worker roles
├── dataset.py          # seeded corpus for each variant
├── targets.py          # the stores that the harness measures
├── settings.py         # run settings that all cases share
├── metrics.py          # latency statistics for one variant
├── environment.py      # machine and source facts recorded with each report
├── progress.py         # live progress during a run
├── report.py           # readable table or JSON output
└── README.md           # how to run and read a benchmark

lib/scripts/
├── test_full.sh                # full suite against disposable containers (needs Docker)
├── benchmark_full.sh           # the benchmarks against those same containers
├── capture_example_output.py   # refresh `examples/expected/`
└── ruff_report.sh              # regenerate `ruff_report.txt`
```

- **Benchmarks stay out of `lib/tests/`,** so a plain `pytest` run never times operations. Their own tests are in `lib/tests/test_benchmarks.py`.
- **The harness uses the standard library only,** like the core package.
- **`benchmark_full.sh` reuses `test_full.sh`.** The container setup is in one place only. `GENIE_TEST_COMMAND` chooses the command that runs against the containers.

## `examples/` — runnable code

```text
examples/
├── basic/              # 17 single-file examples, one topic each
├── advanced/           # 7 single-file examples that combine several ideas
├── lamps/              # 5 package-sized architectural demonstrations
├── expected/           # captured stdout: basic/, advanced/, lamps/
├── _bootstrap.py       # puts `lib/src` and the root on `sys.path` before install
├── _catalog.py         # finds the examples and the location of their output
└── README.md           # reading order and how to run all examples
```

- **All examples run from the repository root.** Single-file examples run as scripts (`python examples/basic/crud_blocks.py`). Lamps are packages and run as modules (`python -m examples.lamps.mem0.demo`).
- **A test checks the output of each example.** `lib/tests/test_examples.py` runs each example and compares all of its standard output with `examples/expected/`. `lib/scripts/capture_example_output.py` refreshes the files after an intended output change.
- **`_catalog.py` finds the examples from the layout:** each `*.py` in `basic/` and `advanced/`, and the `demo.py` of each lamp. The capture script and the test both read it. So the 2 always use the same list of examples.
- **Each new example is also in `RUNNABLE_EXAMPLES`** in `lib/tests/test_examples.py`. That list is explicit. A test asserts that it matches the examples that `_catalog.py` finds.
- **`basic/` has one idea in each file.** `advanced/` combines several ideas. If an example starts to cover 2 topics, the repository splits it into 2 examples and does not combine them.

### A lamp

```text
examples/lamps/<name>/
├── __init__.py         # the public names of the lamp
├── demo.py             # the entry point, run as a module
├── agent.py            # the deterministic agent that runs the demo
├── model.py            # the value types of the lamp
├── structures.py       # which Genie facades the lamp composes
├── <domain>.py         # the logic of the lamp, with file names from its concepts
├── README.md           # concept mapping to the upstream project, and its limits
└── tests/              # the tests of that lamp, in the lamp folder
```

- **Lamps are architectural demonstrations.** They are not compatibility layers and not production implementations. Each lamp keeps the main memory idea of the project that it represents, and one typical workflow.
- **The tests of a lamp stay with the lamp,** in `examples/lamps/<name>/tests` and not in `lib/tests/`.
- **Taxonomy, retrieval, and agent behavior are in the lamp.** That is the purpose of a lamp. It shows the boundary that the core library keeps.

## `design/` — the design notes

- **`design/` holds these notes,** with names in the form `NN-topic.md`. They cover intent, architecture, and organization. They are for the people who build Genie and for the developers who use it.
- **`01-overview.md` and `03-high-level-architecture.md` are the 2 notes for readers.** `README.md` links to them. So when the public memory model, architecture, or safety boundaries change, these 2 notes change too.
- **`04-detailed-architecture.md` holds module internals.** So a change to an algorithm, a check order, or an invariant goes there.
- **Package `README.md` files** hold the details for one area. The design notes link to them and do not repeat them.

## Why every config file sits at the root

| File | Reason it cannot move into `lib/` |
| --- | --- |
| `pyproject.toml` | `readme` and `license-files` resolve in its directory and cannot refer to a path outside it |
| `pyrightconfig.json` | It covers `examples/` and `lib/`. It sets `extraPaths` to `lib/src`, `lib`, and `.`, the same paths as `pytest.ini` |
| `pytest.ini` | It covers `examples/` and `lib/`. It sets `pythonpath` to `lib/src`, `lib`, and `.` |
| `ruff.toml` | Ruff looks for settings in the folder of each file and then in each parent folder. So a config inside `lib/` never applies to `examples/` |

- **All commands run from the repository root:** `python -m pip install -e .`, `python -m pytest`, `python -m ruff check .`, `python -m pyright`.
- **`pyproject.toml` is at the root but refers to the real source** with `[tool.setuptools.packages.find] where = ["lib/src"]`. So the root config and the nested layout agree.

## Where a new file goes

| What you add | Where it goes |
| --- | --- |
| Core library code | `lib/src/genie/<area>/` |
| A public name | the area module, and the root `__all__` in `lib/src/genie/__init__.py` |
| A backend adapter | `lib/src/genie/backend/<family>/` |
| A core test | `lib/tests/` |
| An adapter or contract test | `lib/tests/backends/` |
| A structure facade test | `lib/tests/structures/` |
| A benchmark case | `lib/benchmarks/`, with its test in `lib/tests/test_benchmarks.py` |
| A developer script | `lib/scripts/` |
| A small example | `examples/basic/` or `examples/advanced/`, listed in `RUNNABLE_EXAMPLES` in `lib/tests/test_examples.py`, with output in `examples/expected/` |
| A lamp | `examples/lamps/<name>/`, with tests in `examples/lamps/<name>/tests/` |
| Architecture documentation | `design/03-high-level-architecture.md` or `design/04-detailed-architecture.md`, or the `README.md` of the area |
| A design note | `design/NN-topic.md` |

## Related reading

- [Overview](01-overview.md) — the model that these directories implement
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, the checks to run, and the boundaries for a change
- [High-level architecture](03-high-level-architecture.md) — the components that these directories hold
- [Library](06-library.md) — how an application builds on the package in `lib/src/genie/`
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — reading order and how to run them
