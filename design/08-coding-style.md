# Coding Style

The conventions the Genie code base already follows, and which of them a tool
actually checks. Where a rule is a convention and not a check, the author and
the reviewer keep it.

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
- **Docstrings and annotations are not checked.** No `D1xx` and no `ANN` rules
  are active, and coverage is still complete. That is a review habit, not a
  tool result.
- **`ruff format` is not part of the checks.** Formatting is written by hand.
  Running the formatter would rewrite most of the repository, so leave it alone
  unless the repository adopts it on purpose.
- **A suppression names its rule and its reason.** Every `noqa` comment in the
  repository reads `# noqa: <CODE> - <reason>`, such as
  `# noqa: DTZ001 - invalid input` on a test that passes a naive `datetime` on
  purpose. There are none in `lib/src/genie/`.
- **`./lib/scripts/ruff_report.sh`** writes the grouped output to
  `ruff_report.txt`, which `.gitignore` keeps out of the repository.

## Language level

- **Python 3.11 or newer,** set by `requires-python = ">=3.11"`.
- **Built-in generics and `|` unions only:** `dict[str, Any]`, `str | None`,
  `Mapping[str, int]`. `typing.Optional`, `List`, `Dict`, and `Tuple` never
  appear in the package.
- **3.11 features in use:** `Self` for methods that return their own class,
  `StrEnum` for `HookOperation`, `HandleCapability`, `HookOutcome`,
  `StructureKind`, and `StructureAction`, and `datetime.UTC` for timestamps.
- **Every timestamp is timezone-aware UTC,** built through `utc_now()` in
  `genie.memory.block` rather than `datetime.now()`.
- **No third-party runtime imports in the core.** `lib/benchmarks/` follows the
  same rule. Driver imports belong to the adapters and stay optional.

## Imports

- **`from __future__ import annotations` is the first import** in every module
  that writes annotations. The ones that skip it are the `__init__.py`
  re-export modules plus `memory/errors.py` and `query/scope.py`, which have no
  annotations to defer.
- **Absolute imports only.** Every internal import reads
  `from genie.<area> import <name>`. The package has no relative imports, so a
  module's dependencies are readable without knowing where the file sits.
- **Multi-name imports are parenthesized and sorted,** one name per line.
- **Type-only imports go inside `if TYPE_CHECKING:`,** mostly in adapters that
  need the structure record types for annotations but must not import them at
  runtime.
- **An optional driver is imported inside the function that needs it,** never at
  module scope, so importing the adapter never fails on a missing package.

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
  sections. `Raises:` lists the exceptions a caller has to handle.
- **One blank line after every docstring,** before the first statement.
- **Coverage is complete on the public surface:** every module, every public
  class, and every public function and method has a docstring. The only
  definitions without one are nested closures inside functions.
- **A one-line summary comes first,** then a blank line, then the detail.
- **Exact code names stay in backticks** inside docstrings, the same rule the
  repository applies to its markdown: `` `None` ``, `` `expected_version` ``,
  `` `MemoryPolicy.owner_private()` ``.
- **Plain English, high density.** Short sentences, everyday words, no
  introductory filler. Explain what a name means, never swap the name for a
  friendlier phrase.

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

- **Comments are rare.** The docstrings carry the explanation, and a module as
  large as `sqlite.py` has no comments at all.
- **A comment says why, not what.** It earns its place when the code is correct
  for a reason the reader cannot see, such as
  `# A dropped socket arrives as an OperationalError with no SQLSTATE.`
- **Embedded non-Python code gets a comment block.** Every Lua script in
  `RedisBackend` is preceded by a comment naming its `KEYS` and `ARGV`, what it
  returns, and which Python method it must stay in step with.
- **Long adapters use section banners.** `PostgresBackend`, `Neo4jBackend`,
  `MongoDBBackend`, and `RedisBackend` split their class with a dashed ruler and
  a section name.
- **The sections keep the same order in every adapter:** core block storage and
  ownership traversal, keyed collection, ordered event log, temporal collection
  and index, typed links and lineage, portable search, then the private helpers.
  Keeping the order makes two adapters readable side by side.

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

- **Plain functions, not test classes.** The only classes in the suite are the
  reusable contract suites in `lib/tests/backends/test_backend_contracts.py`,
  the per-adapter subclasses that run them, and the stand-in helpers a test
  needs.
- **No `conftest.py` anywhere, and almost no fixtures.** A test builds what it
  needs in its own body, usually with a bare `Memory()`. Reading a test never
  means chasing shared setup.
- **The test name is a sentence about behavior,** such as
  `test_delete_authorizes_the_target_and_not_its_descendants`.
- **One-line docstring per test** saying what the behavior is.
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
- **Tests stay deterministic.** No clock dependence, no network beyond the gated
  live tests, no reliance on what the machine happens to have installed.

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
