# Software Stack

The languages, packages, servers, and tools Genie uses, and the ones it leaves
out on purpose.

## At a glance

| Layer | Choice | When it is used |
| --- | --- | --- |
| Language | Python 3.11 or newer | Always |
| Core runtime dependencies | None. Standard library only | Always |
| Build | `setuptools>=77`, `setuptools.build_meta` | To build or install |
| Local persistence | `sqlite3`, which ships with Python | No extra package |
| Server adapters | `psycopg`, `neo4j`, `pymongo`, `redis` | Only with the matching extra |
| Lint and import sort | `ruff>=0.16` | `dev` extra |
| Tests | `pytest>=8` | `dev` extra |
| Disposable test servers | Docker | Only for the full suite |
| Benchmarks | Standard library only | No extra package |

## Language and runtime

- **Python 3.11 or newer,** set by `requires-python = ">=3.11"`. The classifiers list 3.11, 3.12, and 3.13.
- **Why 3.11:** the code uses `typing.Self`, `enum.StrEnum`, and `datetime.UTC`, which all arrived in 3.11.
- **The package is typed.** `py.typed` ships with it, and the `Typing :: Typed` classifier says so.

## Core package

- **`dependencies = []`,** by design. Installing `genie` pulls in no third-party package.
- **Standard library modules do the work** that a dependency would do elsewhere:

| Need | Standard library module |
| --- | --- |
| Records and contracts | `dataclasses`, `typing` (`Protocol`, `runtime_checkable`) |
| Timestamps | `datetime`, always timezone-aware UTC through `utc_now()` |
| Ids | `uuid` |
| Stored values and metadata | `json` in the adapters; `base64`, `decimal`, `enum`, and `uuid` in `genie.codecs` |
| Locks inside backends | `threading` |
| Hook context | `contextvars` |
| Local persistence | `sqlite3` |

- **Why no dependencies:** Genie stays independent of the agent framework, the database, the use case, and the value type. A runtime dependency would tie every user to its release cycle and its transitive packages.
- **Why `sqlite3` counts as core:** it ships with Python, so `SQLiteBackend` gives local persistence with no install step and no server.

## Packaging

| Setting | Value | Why |
| --- | --- | --- |
| Build backend | `setuptools.build_meta`, `setuptools>=77` | 77 is the first release that reads `license-files` from `[project]` |
| Source root | `lib/src` | `[tool.setuptools.packages.find] where = ["lib/src"]`, so only `genie*` ships |
| Package data | `py.typed`, `**/README.md` | The module guides travel with the installed package |
| Version | `0.1.0` | Alpha, `Development Status :: 3 - Alpha` |
| License file | `LICENSE` | Named by `license-files`; empty today and needs its text before a release |

- **One `pyproject.toml` at the repository root.** `lib/` has no config of its own.
- **`requirements.txt` declares nothing.** It is a note that points at the `dev` extra.
- **No lock file.** Genie is a library, so it states version ranges and leaves pinning to the application.

## Storage adapters

Every server adapter is an extra. Its driver is imported inside the function
that needs it, so a missing driver raises `UnsupportedOperation` that names the
extra, for example `install 'genie[redis]'`.

| Backend | Extra | Driver range | Server requirement | Image used by `test_full.sh` |
| --- | --- | --- | --- | --- |
| `InMemoryBackend` | — | — | None. In-process | — |
| `SQLiteBackend` | — | built-in `sqlite3` | None. A local file or `:memory:` | — |
| `PostgresBackend` | `postgres` | `psycopg[binary]>=3.1,<4` | A writable primary, not a replica | `postgres:16-alpine` |
| `Neo4jBackend` | `neo4j` | `neo4j>=5.26,<7` | Permission to create and inspect constraints and indexes | `neo4j:5-community` |
| `MongoDBBackend` | `mongodb` | `pymongo>=4.7,<5` | MongoDB 4.4 or newer, as a replica set or a sharded cluster | `mongo:7`, single-member replica set `rs0` |
| `RedisBackend` | `redis` | `redis>=5.0,<6` | One logical database, not a Redis Cluster; `maxmemory-policy noeviction` | `redis:7-alpine`, a primary plus one replica |
| `RoutedBackend` | — | — | Whatever its routed backends need | — |

- **Only the images above are tested.** A server version inside the stated requirement but outside the tested image has no test run behind it.
- **`MongoDBBackend` needs a replica set** because every Genie write is one multi-document transaction, and a standalone `mongod` refuses those. Construction refuses a standalone server unless `verify_transaction_support=False`.
- **`RedisBackend` uses Lua scripts.** Scoped reads run through `EVAL_RO`, which needs Redis 7.0. An older server, a client without `eval_ro`, or a deployment that blocks scripting falls back to the write guard and its retry.
- **`Neo4jBackend` uses a shared bookmark manager** from the 5.x driver, so reads see earlier writes across sessions in a cluster.
- **`PostgresBackend` and `RedisBackend` check the server role** at construction and refuse a replica unless `verify_server_role=False`.
- **Scale-out is out of scope.** The shipped adapters target one small, controlled production database. See [Backends](../lib/src/genie/backend/README.md) for each deployment responsibility.

### Server features left out on purpose

| Feature | Adapter | Reason |
| --- | --- | --- |
| `tsvector`, vector extensions | `PostgresBackend` | Search results stay identical to the reference backend |
| `jsonb` | `PostgresBackend` | It normalizes stored JSON and rejects NUL, so values would not round-trip byte for byte |
| Text index, Atlas Search | `MongoDBBackend` | Search results stay identical to the reference backend |
| Locale collation | `PostgresBackend`, `MongoDBBackend` | Timestamps compare as strings and paging uses `block_id > ...`, so the adapters compare in byte order. Postgres columns use `COLLATE "C"` |
| `WAIT`, `WAITAOF` | `RedisBackend` | A round trip on every write, and not available on every server |
| Redis Cluster | `RedisBackend` | A Genie write spans many keys, which a cluster forbids across hash slots |

- **Portable search scans candidates inside the scope** in every shipped adapter. A large full-text or vector index belongs in a separate `SearchIndex` implementation.

## Developer tooling

| Job | Tool | Config |
| --- | --- | --- |
| Lint and import sort | `ruff check .` | `ruff.toml`: `line-length = 193`, `src = ["lib/src", "lib", "."]` |
| Format | None, by choice | — |
| Type check | None | — |
| Tests | `pytest` | `pytest.ini`: `testpaths` and `pythonpath` |
| Syntax | `python -m compileall` | — |
| Lint report | `lib/scripts/ruff_report.sh` | Writes `ruff_report.txt`, ignored by git |

- **`dev` holds only tools the repository runs:** `pytest` and `ruff`. A tool joins it only when a script or a documented check invokes it.
- **Config sits at the repository root** because it covers `examples/` as well as `lib/`.
- **No CI configuration is in the repository.** The checks run locally, and `test_full.sh` is the complete run.
- **Rules for how the tools are used** are in [Coding style](08-coding-style.md).

## Test infrastructure

- **A plain `pytest` run needs no server.** It runs the core, the reference backend, `SQLiteBackend`, the examples, and the lamps, and skips live adapter tests. Report it as partial.
- **Live adapter tests are gated twice:** they run only when the driver is importable and the `GENIE_*` connection variables are set.

| Adapter | Connection variable | Other variables |
| --- | --- | --- |
| `PostgresBackend` | `GENIE_POSTGRES_DSN` | `GENIE_POSTGRES_SCHEMA` (default `genie_test`) |
| `Neo4jBackend` | `GENIE_NEO4J_URI` | `GENIE_NEO4J_USER`, `GENIE_NEO4J_PASSWORD`, `GENIE_NEO4J_DATABASE` |
| `MongoDBBackend` | `GENIE_MONGODB_URI` | `GENIE_MONGODB_DATABASE` |
| `RedisBackend` | `GENIE_REDIS_URL` | `GENIE_REDIS_USER`, `GENIE_REDIS_PASSWORD`, `GENIE_REDIS_KEY_PREFIX`, `GENIE_REDIS_REPLICA_URL` (replica tests) |

- **`lib/scripts/test_full.sh` is the complete suite.** It needs Docker and the four drivers (`pip install -e ".[dev,mongodb,neo4j,postgres,redis]"`), starts disposable containers bound to `127.0.0.1`, sets the variables, runs `compileall` and then `pytest`, and stops the containers on exit.
- **Test ports stay off the defaults:** `15432`, `17687`, `17017`, `16379`, and `16380` for the Redis replica. Each `GENIE_TEST_*_HOST_PORT` overrides one.
- **Every image is replaceable** through `GENIE_TEST_<SERVER>_IMAGE`, for example `GENIE_TEST_MONGODB_IMAGE`.
- **`GENIE_TEST_PYTHON` and `GENIE_TEST_COMMAND`** choose the interpreter and the command, so the same container setup can run something other than `pytest`.
- **Example output is checked** by `lib/tests/test_examples.py` against `examples/expected/`. `lib/scripts/capture_example_output.py` refreshes it.

## Benchmarks

- **`lib/benchmarks/` uses the standard library only,** like the core. `pytest` does not collect it.
- **`python lib/benchmarks/run.py`** measures `in_memory` and `sqlite` with no server and no extra package.
- **`lib/scripts/benchmark_full.sh`** reuses `test_full.sh` through `GENIE_TEST_COMMAND`, so all six targets run against the same disposable servers.
- **Details** are in [Benchmarks](../lib/benchmarks/README.md).

## Examples and lamps

- **No third-party imports.** Examples and lamps use `genie` and the standard library only, so they run from a fresh checkout.
- **No model calls.** Lamps replace model calls, specialized indexes, and production storage with plain application code, so their output is stable and testable.

## Not in the stack

| Left out | Why |
| --- | --- |
| Agent frameworks | Genie stays independent of how an application runs its agents |
| LLM or embedding SDKs | Genie stores values. It does not create embeddings or call models |
| A bundled vector store or search engine | Search is a `SearchIndex` capability that an application may supply |
| A default server database | Server adapters are extras. The defaults are in-process or a local file |
| An ORM or query builder | Each adapter writes the queries its store understands, and keeps the exact schema checks |
| A formatter | `ruff format` would rewrite most files and bury small changes in diff noise |
| A type checker | Types are kept in review. None is configured |

## Adding to the stack

- **A new server adapter** gets its own extra with a bounded version range, imports its driver inside the functions that use it, and raises `UnsupportedOperation` when the driver is missing.
- **Its live tests** are gated on the driver and on `GENIE_<AREA>_*` variables, and `test_full.sh` gains a disposable container for it.
- **A new developer tool** joins `dev` only when something in the repository invokes it.
- **The core keeps `dependencies = []`.**

## Related reading

- [Overview](01-overview.md) — the design philosophy behind these choices
- [High-level architecture](03-high-level-architecture.md) — where backends and indexes attach
- [Repository organization](07-repo-organization.md) — where each file lives
- [Coding style](08-coding-style.md) — how the tools are used
- [Backends](../lib/src/genie/backend/README.md) — adapter behavior and deployment responsibilities
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup and the checks to run
