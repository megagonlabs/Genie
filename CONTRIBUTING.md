# Contributing to Genie

Change that improves the memory model or the
public interfaces is welcome.

## Set up

- **Python 3.11 or newer.** The core package has no third-party runtime
  dependencies, so a plain virtual environment is enough.

```bash
python -m venv .venv --prompt genie
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
```

- **`dev` installs `pytest` and `ruff`,** the only two tools this repository
  runs.
- **Backend adapters are extras.** Add the ones you are working on:
  `python -m pip install -e ".[dev,mongodb,neo4j,postgres,redis]"`.

## Run the checks

```bash
python -m pytest
python -m ruff check .
python -m compileall -q lib/src/genie lib/tests examples
```

- **`./lib/scripts/ruff_report.sh`** regenerates `ruff_report.txt` at the
  repository root.
- **`python lib/scripts/capture_example_output.py`** refreshes the captured
  output of every example under `examples/expected/`. Run it when you change
  what an example prints, and commit the result.
- **Keep `ruff check .` clean.** Line length is 193, set in `ruff.toml`.
  Ruff also sorts imports, so there is no separate formatter to run.

## Tests that need a server

A plain `pytest` run **skips** the live tests of every adapter that needs a
running server. Those tests run only when the adapter's optional driver is
installed and its `GENIE_*` connection variables are set.

- **Report a plain run as partial,** not as a full pass. It looks like
  `1133 passed, 220 skipped`.
- **`./lib/scripts/test_full.sh` runs everything** against disposable
  PostgreSQL, Neo4j, MongoDB, and Redis containers. It needs Docker, finishes in
  under two minutes, and gives `1353 passed`. It removes its containers when it exits.
- **The script uses the Python already on your `PATH`,** so activate your
  environment first. `GENIE_TEST_PYTHON` overrides that interpreter and
  `GENIE_TEST_COMMAND` overrides what runs once the servers are ready.

Run the full script before sending a change that touches any adapter, the
backend contracts, or concurrency behavior.

## Benchmarks

```bash
python lib/benchmarks/run.py
python lib/benchmarks/run.py --workers 4
```

`./lib/scripts/benchmark_full.sh` runs the same harness against throwaway
containers. See `lib/benchmarks/README.md`.

## What belongs in Genie

Genie provides typed building blocks for developers who build their own memory
systems. It stays independent of any agent framework, database, memory taxonomy,
retrieval strategy, and stored value type.

- **Keep the core package free of third-party runtime dependencies.**
- **Storage adapters live behind the backend contracts,** stay optional, and
  guard their third-party imports. A missing driver raises
  `UnsupportedOperation`.
- **Facades depend on narrow backend capability protocols,** not concrete
  backend classes. Do not assume every backend implements every optional
  capability.
- **Search is a backend capability** (`SearchIndex`) with portable query and
  result types in `genie.query`. It is not a separate integration point.
- **Parent/child relationships mean ownership** and drive scope and lifecycle.
  Links and mounts describe relationships that do not imply ownership.
- **Reuse existing facades** when building higher-level structures instead of
  adding a duplicate storage model.

Keep application business logic, memory taxonomies, agent behavior, storage
layout, retrieval strategy, and domain-specific value meaning **outside** the
core library. Task interpretation, action selection, planning, extraction,
consolidation, and ranking policy belong in an application or agent. Agent
framework integrations are fine in `examples/`, including `examples/lamps/`, but they must never
become dependencies of the core package.

## Engineering standards

Apply these in proportion to the size and user-facing impact of the change.

- **Prefer focused, minimal changes** that fit the existing architecture.
- **Keep public APIs clear and typed,** with Google-style docstrings and
  predictable exceptions where they affect users.
- **Every public name is exported from `genie` itself,** and also from its
  focused subpackage. `test_public_imports.py` enforces this, so a new public
  name must be added to the root `__all__` too.
- **Write plain English** in docs, docstrings, and comments. Avoid fancy phrasing
  where a direct word works. Keep exact code names intact and in backticks.
- **Prefer explicit, least-privilege behavior** for scoped memory access and
  policy checks.
- **Add or update tests** when behavior, public APIs, edge cases, or regressions
  are affected.
- **Add backend contract and concurrency tests** when backend capability
  behavior or atomicity changes. When an operation needs atomic behavior that a
  portable read-then-write cannot provide safely, define or reuse an atomic
  backend capability.
- **Avoid hidden dependencies, global state, and non-deterministic behavior** in
  tests and examples. A test must not depend on whether the machine happens to
  have a server configured.
- **Update examples and documentation** when user-facing behavior changes, and
  keep `design/01-overview.md` and `design/03-high-level-architecture.md` in
  sync with the public memory model, architecture, and safety boundaries.
- **Avoid broad refactors, dependency additions, and compatibility breaks**
  unless they are directly required, and say why in the change.

## Where things go

| Path | Holds |
| --- | --- |
| `lib/src/genie/` | The core library |
| `lib/tests/` | Core library tests |
| `lib/tests/backends/` | Adapter tests and the reusable backend contract tests |
| `lib/benchmarks/` | The benchmark harness, standard library only |
| `lib/tests/test_benchmarks.py` | Tests for that harness |
| `lib/scripts/` | Repository-wide developer scripts |
| `pyproject.toml` | The `genie` package metadata, extras, and build settings |
| `pytest.ini` | Repo-wide `pytest` settings |
| `ruff.toml` | Repo-wide `ruff` settings |
| `examples/basic/`, `examples/advanced/` | Small runnable examples |
| `examples/lamps/<name>/`, `examples/lamps/<name>/tests/` | One architectural demonstration each |
| `examples/expected/` | Captured output of every example |
| `design/` | Design notes, numbered `NN-topic.md`: the model, the architecture, and the safety boundaries |

- **Every config file sits at the repository root.** `pyproject.toml` describes
  the `genie` package and points at the source with
  `[tool.setuptools.packages.find] where = ["lib/src"]`. It has to be at the
  root, because `readme` and `license-files` are resolved next to it and cannot
  point outside its directory. `pytest.ini` and `ruff.toml` are there for their
  own reasons: they cover `examples/` as well as `lib/`, and `ruff` finds its
  settings by walking up from each file. Install the package with
  `python -m pip install -e .`, and run `pytest` and `ruff` from the repository
  root.
- **The benchmark harness stays out of `lib/tests/`** so a plain `pytest` run
  does not time operations.
- **Examples run from the repository root** and use plain assertions. Keep them
  useful for a first-time reader.
- **Every example is output-tested.** `lib/tests/test_examples.py` runs each one
  and compares its whole standard output against `examples/expected/`. Add new
  examples to its `RUNNABLE_EXAMPLES` list and capture their output, and keep
  them deterministic so the comparison stays stable.
- **Lamps are architectural demonstrations,** not compatibility layers or
  production implementations. Document their concept mappings and their
  deliberate limits.

## Sending a change

- **Branch off `dev`.**
- **Run `python -m pytest` and `python -m ruff check .`,** plus
  `./lib/scripts/test_full.sh` when your change touches an adapter.
- **Say what you ran** in the pull request, and mark a plain `pytest` run as
  partial.
- **Open the pull request against `dev`.**
