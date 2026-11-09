# Software Stack

This note lists the languages, packages, servers, and tools that Genie uses. It
also lists the ones that Genie leaves out on purpose.

## At a glance

| Layer | Choice | When it is used |
| --- | --- | --- |
| Language | Python 3.12 or 3.13 | Always |
| Core runtime dependencies | None. Standard library only | Always |
| Build | `setuptools>=77`, `setuptools.build_meta` | To build or install |
| Local persistence | `sqlite3`, which is part of Python | No extra package |
| Server adapters | `psycopg`, `neo4j`, `pymongo`, `redis` | Only with the extra for that adapter |
| Lint and import sort | `ruff>=0.16` | `dev` extra |
| Type check | `pyright[nodejs]>=1.1.414` | `dev` extra |
| Tests | `pytest>=8` | `dev` extra |
| Disposable test servers | Docker | Only for the full suite |
| Benchmarks | Standard library only | No extra package |

## Language and runtime

- **Python 3.12 or 3.13,** set by `requires-python = ">=3.12,<3.14"`. The classifiers list 3.12 and 3.13.
- **Why 3.12 and 3.13:** these are the versions on which the whole DI 1.0 family installs and runs together. The code itself uses `typing.Self`, `enum.StrEnum`, and `datetime.UTC`, which Python 3.11 added.
- **The package is typed.** The package includes `py.typed`, and the `Typing :: Typed` classifier states this. Pyright checks the annotations.

## Core package

- **`dependencies = []`** is a design decision. An install of `genie` adds no third-party package.
- **Standard library modules do the work** that a dependency does in other projects:

| Need | Standard library module |
| --- | --- |
| Records and contracts | `dataclasses`, `typing` (`Protocol`, `runtime_checkable`) |
| Timestamps | `datetime`, always timezone-aware UTC through `utc_now()` |
| Ids | `uuid` |
| Stored values and metadata | `json` in the adapters. `base64`, `decimal`, `enum`, and `uuid` in `genie.codecs` |
| Locks inside backends | `threading` |
| Hook context | `contextvars` |
| Local persistence | `sqlite3` |

- **Why no dependencies:** Genie stays independent of the agent framework, the database, the use case, and the value type. A runtime dependency ties each user to the release cycle and the transitive packages of that dependency.
- **Why `sqlite3` counts as core:** it is part of Python. So `SQLiteBackend` gives local persistence with no install step and no server.

## Packaging

| Setting | Value | Why |
| --- | --- | --- |
| Build backend | `setuptools.build_meta`, `setuptools>=77` | 77 is the first release that reads `license` (an SPDX expression) and `license-files` from `[project]` |
| Source root | `lib/src` | `[tool.setuptools.packages.find] where = ["lib/src"]`, so only `genie*` packages ship |
| Package data | `py.typed`, `**/README.md` | The installed package includes the module guides |
| Version | `1.0.0` |  |
| License file | `LICENSE` | `license-files` names it. It holds the BSD 3-Clause text for the `1.0.0` release |
| License expression | `LicenseRef-Megagon-BSD-3-Clause-Attribution` | `license` gives it. The text adds an attribution clause (clause 4) to BSD 3-Clause, so the SPDX identifier `BSD-3-Clause` does not match it. The wheel `METADATA` has it as `License-Expression:` |

- **One `pyproject.toml` at the repository root.** `lib/` has no config of its own.
- **`requirements.txt` declares nothing.** It is a note that refers to the `dev` extra.
- **No lock file.** Genie is a library. It states version ranges, and the application pins exact versions.

## Storage adapters

Each server adapter is an extra. The adapter imports its driver inside the
function that needs it. If the driver is not installed, the call raises
`UnsupportedOperation` with the command that installs the extra, for example
`python -m pip install -e '.[redis]'`.

| Backend | Extra | Driver range | Server requirement | Image used by `test_full.sh` |
| --- | --- | --- | --- | --- |
| `InMemoryBackend` | — | — | None. In-process | — |
| `SQLiteBackend` | — | built-in `sqlite3` | None. A local file or `:memory:` | — |
| `PostgresBackend` | `postgres` | `psycopg[binary]>=3.1,<4` | A writable primary, not a replica | `postgres:16-alpine` |
| `Neo4jBackend` | `neo4j` | `neo4j>=5.26,<7` | Neo4j server 5.7 or newer. Permission to create and inspect constraints and indexes | `neo4j:5-community` |
| `MongoDBBackend` | `mongodb` | `pymongo>=4.7,<5` | MongoDB 4.4 or newer, as a replica set or a sharded cluster | `mongo:7`, single-member replica set `rs0` |
| `RedisBackend` | `redis` | `redis>=5.0,<6` | One logical database, not a Redis Cluster. `maxmemory-policy noeviction` | `redis:7-alpine`, a primary plus one replica |
| `RoutedBackend` | — | — | The needs of its routed backends | — |

- **Only the images above are tested.** If a server version is inside the stated requirement but is not a tested image, no test run covers it.
- **`MongoDBBackend` needs a replica set.** Each Genie write is one multi-document transaction, and a standalone `mongod` refuses these transactions. The constructor refuses a standalone server. To skip this check, set `verify_transaction_support=False`.
- **`RedisBackend` uses Lua scripts.** Scoped reads run through `EVAL_RO`, which needs Redis 7.0. Some servers cannot run these reads: an older server, a client without `eval_ro`, or a deployment that blocks scripts. On these servers, the reads use the mutation guard and its retry.
- **`Neo4jBackend` uses a shared bookmark manager** from the 5.x driver. So in a cluster, reads see earlier writes from other sessions.
- **`PostgresBackend`, `MongoDBBackend`, and `RedisBackend` check the server role** in the constructor. `PostgresBackend` and `RedisBackend` refuse a replica. `MongoDBBackend` refuses a node that does not accept writes, for example a direct connection to a secondary. To skip this check, set `verify_server_role=False`.
- **Scale-out is out of scope.** The shipped adapters target one small, controlled production database. [Backends](../lib/src/genie/backend/README.md) lists each deployment responsibility.

### Server features left out on purpose

| Feature | Adapter | Reason |
| --- | --- | --- |
| `tsvector`, vector extensions | `PostgresBackend` | Search results stay identical to the results of the reference backend |
| `jsonb` | `PostgresBackend` | It normalizes stored JSON and rejects NUL. With `jsonb`, values do not round-trip byte for byte |
| Text index, Atlas Search | `MongoDBBackend` | Search results stay identical to the results of the reference backend |
| Locale collation | `PostgresBackend`, `MongoDBBackend` | Timestamps compare as strings, and page queries use `block_id > ...`. So the adapters compare in byte order. Postgres columns use `COLLATE "C"` |
| `WAIT`, `WAITAOF` | `RedisBackend` | They add a round trip to each write, and some servers do not have them |
| Redis Cluster | `RedisBackend` | A Genie write spans many keys. A cluster forbids this across hash slots |

- **Portable search scans candidates inside the scope** in each shipped adapter. A large full-text or vector index belongs in a separate `SearchIndex` implementation.

## Developer tooling

| Job | Tool | Config |
| --- | --- | --- |
| Lint and import sort | `ruff check .` | `ruff.toml`: `line-length = 193`, `src = ["lib/src", "lib", "."]`, `extend-select = ["DTZ"]` |
| Format | None, by choice | — |
| Type check | `python -m pyright` | `pyrightconfig.json`: `include`, `extraPaths`, `pythonVersion = "3.12"`, `typeCheckingMode = "standard"` |
| Tests | `pytest` | `pytest.ini`: `testpaths` and `pythonpath` |
| Syntax | `python -m compileall` | — |
| Lint report | `lib/scripts/ruff_report.sh` | Writes `ruff_report.txt`. Git ignores this file |

- **`dev` holds only tools that the repository runs:** `pytest`, `ruff`, and `pyright`. A tool goes into `dev` only if a script or a documented check uses it.
- **`pyright` comes with its `nodejs` extra.** That extra installs Node.js as a wheel. So a Pyright run needs no system Node.js.
- **Pyright needs the 4 adapter extras.** It checks the adapter code against the types of each driver. If a driver is not installed, Pyright reports its imports as errors.
- **`pythonVersion = "3.12"`** is the oldest version in `requires-python`. So Pyright reports a call to a newer standard library function that has no version check.
- **Config is at the repository root** because it covers `examples/` and `lib/`.
- **No CI configuration is in the repository.** The checks run locally, and `test_full.sh` is the complete run.
- **Rules for the use of the tools** are in [Coding style](08-coding-style.md).

## Test infrastructure

- **A plain `pytest` run needs no server.** It runs the tests for the core, the reference backend, `SQLiteBackend`, the examples, and the lamps. It skips the live adapter tests. So the result is a partial run.
- **Live adapter tests have 2 gates:** they run only if the driver is importable and the `GENIE_*` connection variables are set.

| Adapter | Connection variable | Other variables |
| --- | --- | --- |
| `PostgresBackend` | `GENIE_POSTGRES_DSN` | `GENIE_POSTGRES_SCHEMA` (default `genie_test`) |
| `Neo4jBackend` | `GENIE_NEO4J_URI` | `GENIE_NEO4J_USER`, `GENIE_NEO4J_PASSWORD`, `GENIE_NEO4J_DATABASE` |
| `MongoDBBackend` | `GENIE_MONGODB_URI` | `GENIE_MONGODB_DATABASE` |
| `RedisBackend` | `GENIE_REDIS_URL` | `GENIE_REDIS_USER`, `GENIE_REDIS_PASSWORD`, `GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_REPLICA_URL` (replica tests) |

- **`lib/scripts/test_full.sh` is the complete suite.** It needs Docker and the 4 drivers (`pip install -e ".[dev,mongodb,neo4j,postgres,redis]"`). It runs `compileall` and `pyright`, then starts disposable containers bound to `127.0.0.1`. Then it sets the variables and runs `pytest`. It stops the containers on exit.
- **Test ports are not the default ports:** `15432`, `17687`, `17017`, `16379`, and `16380` for the Redis replica. Each `GENIE_TEST_*_HOST_PORT` variable overrides one port.
- **Each image is replaceable** through `GENIE_TEST_<SERVER>_IMAGE`, for example `GENIE_TEST_MONGODB_IMAGE`.
- **`GENIE_TEST_PYTHON` and `GENIE_TEST_COMMAND`** choose the interpreter and the command. So the same container setup can run a command other than `pytest`.
- **`lib/tests/test_examples.py` checks the example output** against `examples/expected/`. `lib/scripts/capture_example_output.py` refreshes the files there.

## Benchmarks

- **`lib/benchmarks/` uses the standard library only,** like the core. `pytest` does not collect it.
- **`python lib/benchmarks/run.py`** measures `in_memory` and `sqlite` with no server and no extra package.
- **`lib/scripts/benchmark_full.sh`** reuses `test_full.sh` through `GENIE_TEST_COMMAND`. So all 6 targets run against the same disposable servers.
- **Details** are in [Benchmarks](../lib/benchmarks/README.md).

## Examples and lamps

- **No third-party imports.** Examples and lamps use `genie` and the standard library only. So they run from a new checkout.
- **No model calls.** Lamps use plain application code in place of model calls, specialized indexes, and production storage. So their output is stable and testable.

## Not in the stack

| Left out | Why |
| --- | --- |
| Agent frameworks | Genie stays independent of how an application runs its agents |
| LLM or embedding SDKs | Genie stores values. It does not create embeddings or call models |
| A bundled vector store or search engine | Search is a `SearchIndex` capability that an application can supply |
| A default server database | Server adapters are extras. The defaults are in-process or a local file |
| An ORM or query builder | Each adapter writes the queries that its store understands. It also keeps the exact schema checks |
| A formatter | A `ruff format` run rewrites most files. Small changes then become hard to find in the diff |

## Adding to the stack

- **A new server adapter** gets its own extra with a bounded version range. It imports its driver inside the functions that use the driver. It raises `UnsupportedOperation` if the driver is not installed.
- **Its live tests** have gates on the driver and on `GENIE_<AREA>_*` variables. `test_full.sh` gets a disposable container for the new adapter.
- **A new developer tool** goes into `dev` only if a file in the repository uses it.
- **The core keeps `dependencies = []`.**

## Related reading

- [Overview](01-overview.md) — the design philosophy for these choices
- [High-level architecture](03-high-level-architecture.md) — where backends and indexes connect
- [Library](06-library.md) — how an application builds its own memory on this stack
- [Repository organization](07-repo-organization.md) — the location of each file
- [Coding style](08-coding-style.md) — the use of the tools
- [Backends](../lib/src/genie/backend/README.md) — adapter behavior and deployment responsibilities
- [CONTRIBUTING.md](../CONTRIBUTING.md) — the setup and the checks
