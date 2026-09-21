# Coding Style

The conventions the Genie code base already follows, so a new change can look
like the code around it.

Two kinds of thing are written down here and they carry different weight. A few
are hard rules, because `ruff check .` fails or `CONTRIBUTING.md` requires them,
and the text says which. The rest are house habits: worth matching so the code
stays consistent, but not a reason to reject a sensible alternative. Where the
code base itself varies, this note says so instead of picking a winner.

## What the tools check

```bash
python -m pytest
python -m ruff check .
python -m compileall -q lib/src/genie lib/tests examples
```

- **`ruff.toml` sets two things only:** `line-length = 193` and
  `src = ["lib/src", "lib", "."]`. Everything else is ruff's own default rule
  selection, so the active set moves with the ruff version in the `dev` extra.
- **Import sorting is checked.** `I001` is active, so a badly ordered import
  block fails `ruff check .`. There is no separate `isort` step.
- **Line length is not checked.** `E501` is not in the active set, so 193 is a
  budget the author keeps by hand. No line in the package reaches it.
- **Docstrings and annotations are not checked by ruff.** No `D1xx` and no
  `ANN` rules are active. `CONTRIBUTING.md` still asks for typed public APIs
  with Google-style docstrings, so that one is kept in review instead.
- **`ruff format` is not part of the checks.** Formatting is written by hand.
  Running the formatter would rewrite most of the repository, which would bury
  a small change in unrelated diff noise.
- **A suppression names its rule and its reason.** Every `noqa` comment in the
  repository reads `# noqa: <CODE> - <reason>`, such as
  `# noqa: DTZ001 - invalid input` on a test that passes a naive `datetime` on
  purpose. They all sit in tests and benchmarks so far; the package has none.
- **`./lib/scripts/ruff_report.sh`** writes the grouped output to
  `ruff_report.txt`, which `.gitignore` keeps out of the repository.

## Language level

- **Python 3.11 or newer,** set by `requires-python = ">=3.11"`.
- **Built-in generics and `|` unions:** `dict[str, Any]`, `str | None`,
  `Mapping[str, int]`.
- **3.11 features in use:** `Self` for methods that return their own class,
  `StrEnum` for `HookOperation`, `HandleCapability`, `HookOutcome`,
  `StructureKind`, and `StructureAction`, and `datetime.UTC` for timestamps.
- **Every timestamp is timezone-aware UTC,** built through `utc_now()` in
  `genie.memory.block` rather than `datetime.now()`.
- **No third-party runtime imports in the core,** a hard rule from
  `CONTRIBUTING.md`. `lib/benchmarks/` follows it too. Driver imports belong to
  the adapters and stay optional.

## Imports

- **`from __future__ import annotations` is the first import** in every module
  that writes annotations. The ones that skip it are the `__init__.py`
  re-export modules plus `memory/errors.py` and `query/scope.py`, which have no
  annotations to defer.
- **Internal imports are absolute.** Every one reads
  `from genie.<area> import <name>`. The package has no relative imports, so a
  module's dependencies are readable without knowing where the file sits.
- **Multi-name imports are parenthesized with one name per line,** which is
  the shape ruff's import sort produces.
- **Type-only imports go inside `if TYPE_CHECKING:`,** mostly in adapters that
  need the structure record types for annotations but must not import them at
  runtime.
- **An optional driver is imported inside the function that needs it,** not at
  module scope, so importing the adapter never fails on a missing package.
  `CONTRIBUTING.md` makes this a hard rule: a missing driver has to raise
  `UnsupportedOperation`.

## Package surface

- **Every module opens with a docstring.** A large adapter uses labelled
  paragraphs (`Storage layout:`, `Writes:`, `Reads:`, `Search:`) to explain its
  strategy once at the top instead of repeating it per method.
- **`__init__.py` re-exports and nothing else.** It carries a one-line
  docstring, the imports, and a sorted `__all__`.
- **Every public name is in the root `__all__` too,** and
  `lib/tests/test_public_imports.py` fails when it is missing. A name is
  reachable both as `from genie import X` and `from genie.<area> import X`.
- **A leading underscore marks private.** `structures/_hooks.py` and
  `structures/_routing.py` are internal and are not re-exported.

## Types and data shapes

| Pattern | Used for |
| --- | --- |
| `@dataclass(frozen=True, slots=True)` | every record Genie stores or returns |
| `Protocol` with `@runtime_checkable` | every backend, access, and codec contract |
| `__slots__` on a plain class | facades and other hot objects |
| Keyword-only arguments | optional parameters on public methods |
| `TypeAlias` | shared vocabulary such as `BlockRef` and `LinkDirection` |

- **Records are frozen and slotted.** `MemoryBlock`, `MemoryLink`,
  `SearchResult`, `KeyBinding`, and the rest are immutable. A change is a copy:
  `MemoryBlock.with_updates(**changes)` wraps `dataclasses.replace`.
- **Contracts are protocols, not base classes.** An adapter satisfies
  `MemoryBackend` structurally, and `@runtime_checkable` lets a test assert
  `isinstance(backend, KeyedStore)` without an import cycle.
- **A protocol method body is `...`,** after its docstring and the blank line.
- **Optional public parameters are keyword-only.** `Memory.put` takes every
  argument after `self` by keyword, so a new parameter never changes the meaning
  of a positional call.
- **A `TypeAlias` gets its own docstring** on the line below, so the alias shows
  up in generated documentation.

## Docstrings

- **Google style,** with `Args:`, `Returns:`, `Raises:`, and `Attributes:`
  sections. `CONTRIBUTING.md` requires this for public APIs. `Raises:` lists the
  exceptions a caller has to handle.
- **A blank line follows the docstring,** before the first statement. The code
  base does this everywhere.
- **Coverage is complete on the public surface:** every module, every public
  class, and every public function and method has a docstring. The only
  definitions without one are nested closures inside functions.
- **A one-line summary comes first,** then a blank line, then the detail.
- **Exact code names stay in backticks** inside docstrings, the same rule the
  repository applies to its markdown: `` `None` ``, `` `expected_version` ``,
  `` `MemoryPolicy.owner_private()` ``.
- **Plain English, high density,** which `CONTRIBUTING.md` asks for in docs,
  docstrings, and comments alike. Short sentences, everyday words, no
  introductory filler. Explain what a name means, and keep the name itself.

```python
def get_block(self, block_id: str) -> MemoryBlock | None:
    """Fetch a block by id.

    Args:
        block_id: Memory block id.

    Returns:
        Stored block, or `None` when it does not exist.
    """

    ...
```

## Comments

- **Comment as much as the code needs.** The repository varies widely and that
  is fine. `RedisBackend` is commented heavily, because its Lua scripts, its
  retry loop, and its key layout are not self-evident. `SQLiteBackend` has
  almost none, because the SQL it runs reads plainly. Neither one is a target to
  copy.
- **Docstrings and comments do different jobs.** A docstring is the contract a
  caller reads without opening the body. A comment is for whoever is reading the
  body. Writing both for the same function is normal, not duplication.
- **A comment is most useful for what the code cannot show,** such as why a
  check is ordered the way it is, what a driver's reply means, or which
  invariant makes a branch unreachable:
  `# A dropped socket arrives as an OperationalError with no SQLSTATE.`
- **Embedded non-Python code gets a comment block.** Every Lua script in
  `RedisBackend` is preceded by a comment naming its `KEYS` and `ARGV`, what it
  returns, and which Python method it must stay in step with.
- **The large adapters split their class with section banners,** a dashed ruler
  and a section name. `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and
  `RedisBackend` use the same section order: core block storage and ownership
  traversal, keyed collection, ordered event log, temporal collection and index,
  typed links and lineage, portable search, then the private helpers. Matching
  it lets two adapters be read side by side.

```python
    # ------------------------------------------------------------------
    # Core block storage and ownership traversal
```

## Errors and validation

- **Validate arguments at the boundary.** Public constructors and methods check
  their inputs before doing any work. `TypeError` for the wrong type,
  `ValueError` for the wrong value.
- **A message is a full sentence that starts with the parameter name** and ends
  with a period: `"url must be a string."`, `"content_type must not be empty."`.
  Nearly every literal message in the package follows this, and the tests match
  on the message text.
- **Genie exceptions descend from `GenieError`.** `MemoryNotFound`,
  `MemoryConflict`, `UnsupportedOperation`, `HookRecursionError`, and the
  `BackendError` family, which carries a `retryable` attribute documented in its
  `Attributes:` section.
- **Chain the original failure** with `raise ... from error`, so the driver
  traceback survives.
- **A missing driver raises `UnsupportedOperation` and names the extra:**
  `"RedisBackend requires the optional 'redis' package; install 'genie[redis]'."`

## Naming

| Kind | Convention | Examples |
| --- | --- | --- |
| Storage capability protocol | `<Thing>Store` or `<Thing>Index` | `BlockStore`, `KeyedStore`, `SearchIndex`, `TraversalIndex` |
| Access protocol for facades | `<Facade>Access`, or `<Role>Reader` / `<Role>Writer` | `OwnershipAccess`, `GraphAccess`, `BlockReader`, `LinkWriter` |
| Structure facade | Plain noun for the shape it exposes | `OwnershipTree`, `KeyedCollection`, `EventLog` |
| Backend adapter | `<Store>Backend` | `SQLiteBackend`, `MongoDBBackend`, `RoutedBackend` |
| Private module constant | `_UPPER_SNAKE` | `_SCHEMA_VERSION`, `_MAX_BACKOFF_SECONDS` |
| Private helper | `_lower_snake` | `_block_id`, `_copy_metadata` |
| Test stand-in class | `_Fake<Thing>` | `_FakeWatchError`, `_FakeClientModule` |
| Test spy hook | `Recording<Thing>Hook` | `RecordingDenialHook`, `RecordingSinkHook` |
| Environment variable | `GENIE_<AREA>_<SETTING>` | `GENIE_REDIS_URL`, `GENIE_POSTGRES_DSN` |

- **`BUILTIN_OPERATION_NAMES` and `OPERATOR_ALIASES` are the only public module
  constants.** Everything else that looks like a constant is private.

## Tests

- **Most tests are plain functions.** Classes are used where they earn their
  place: the reusable contract suites in
  `lib/tests/backends/test_backend_contracts.py`, the per-adapter subclasses
  that run them, and the stand-in helpers a test needs.
- **Setup is usually inline.** The repository has no `conftest.py` and few
  fixtures, so a test builds what it needs in its own body, often with a bare
  `Memory()`. A fixture is the right call when setup is genuinely shared or
  expensive.
- **The test name is a sentence about behavior,** such as
  `test_delete_authorizes_the_target_and_not_its_descendants`.
- **A test usually carries a one-line docstring** saying what the behavior
  is.
- **`pytest.mark.parametrize` takes a tuple of names,** mostly for tables of
  rejected constructor arguments and their expected messages.
- **Live adapter tests are gated twice,** on the driver being importable and on
  the `GENIE_*` connection variables being set:

```python
_LIVE_DRIVER_AVAILABLE = importlib.util.find_spec("psycopg") is not None
_LIVE_REASON = "set GENIE_POSTGRES_DSN and install the postgres extra to run live PostgreSQL tests"
```

- **A plain `pytest` run is therefore partial,** and is reported that way.
  `./lib/scripts/test_full.sh` is the complete run.
- **Tests stay deterministic,** another hard rule from `CONTRIBUTING.md`. No
  clock dependence, no network beyond the gated live tests, and no reliance on
  what the machine happens to have installed.

## Examples

- **The module docstring names the run command,** indented as a code block:

```python
"""Create, read, update, traverse, and recursively delete memory blocks.

Run from the repository root:

    python examples/basic/crud_blocks.py
"""
```

- **Single-file examples carry the bootstrap preamble** that puts the repository
  on `sys.path`, so they run from a checkout with nothing installed. Every file
  in `basic/` and `advanced/` has it, and the `from genie import ...` line sits
  below it. `lib/benchmarks/run.py` does the same for the `benchmarks` package.
  Those files are the only place where a module-level import is not at the top,
  and each one carries a comment saying why it cannot move.
- **Lamp demos skip the preamble** and import `examples.lamps.<name>....`
  directly, because they run as modules with `python -m`.
- **The body is one `main()` with a docstring,** guarded by
  `if __name__ == "__main__": main()`.
- **`print` shows the result, `assert` proves it.** The printed lines are the
  tested output; the assertions state the guarantee the example is about.
- **Output must be stable,** because `lib/tests/test_examples.py` compares the
  whole standard output against `examples/expected/`.

## Scripts and the benchmark harness

- **Shell scripts are bash with `set -euo pipefail`,** a comment block at the
  top explaining what they do and which `GENIE_*` variables change their
  behavior, and a `cd` to the repository root computed from `BASH_SOURCE`.
- **A long-running script prints live progress,** because a silent gap reads as
  a hang.
- **`lib/benchmarks/` uses the standard library only,** like the core package.
  Its entry point lists its own command lines in the module docstring and states
  what the exit status means.

## Related reading

- [Repository organization](07-repo-organization.md) — where a new file goes
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, the checks to run, and the boundaries a change must respect
- [Detailed architecture](04-detailed-architecture.md) — the invariants behind some of these conventions
- [Overview](01-overview.md) — the design philosophy the style serves
