# Coding Style

This note lists the conventions that the Genie code base follows. A new change
can use them to look like the code around it.

The note contains 2 kinds of conventions, and they have different weight:

- **Hard rules.** `ruff check .` or `pyright` fails, or `CONTRIBUTING.md` requires them. The
  text says which conventions are hard rules.
- **House habits.** Code that matches them stays consistent. But a habit is not
  a reason to reject a sensible alternative.
- **Variation.** If the code base itself varies, this note says so. It does not
  choose one way as the standard.

## What the tools check

| Job | Tool | Config file |
| --- | --- | --- |
| Lint and import sort | `ruff check .`, from `ruff>=0.16` in the `dev` extra | `ruff.toml`, at the repository root |
| Format | none, by choice | — |
| Type check | `pyright`, from `pyright[nodejs]>=1.1.414` in the `dev` extra | `pyrightconfig.json`, at the repository root |
| Tests | `pytest`, from `pytest>=8` in the `dev` extra | `pytest.ini`, at the repository root |
| Syntax | `compileall`, from the standard library | — |

```bash
python -m pytest
python -m ruff check .
python -m pyright
python -m compileall -q lib/src/genie lib/tests examples
```

- **`ruff.toml` sets `line-length`, `src`, `extend-select`, and `ignore`.**
  All other settings are ruff defaults. So the active rule set, except the
  `DTZ` rules and the ignored `UP` rules, changes with the ruff version in
  the `dev` extra.
- **`UP040`, `UP046`, and `UP047` are off.** A `type` alias is a
  `TypeAliasType`, so public aliases such as `BlockRef` fail in
  `isinstance()`. The code also keeps one `TypeVar` style.
- **All `DTZ` rules are active.** Genie requires timezone-aware datetimes.
  The default rule set of ruff changes between versions, so `extend-select`
  turns on all `DTZ` rules explicitly.
- **Ruff checks the import order.** `I001` is active, so an import block in the
  wrong order fails `ruff check .`. There is no separate `isort` step.
- **Ruff does not check the line length.** `E501` is not in the active set. So
  the author keeps each line inside the budget of 193 by hand. No line in the
  package reaches it.
- **Pyright checks the types in `standard` mode.** It checks `lib/` and
  `examples/` against Python 3.12. It needs the 4 adapter extras, because it
  checks the adapter code against the types of each driver.
- **No tool checks that annotations and docstrings exist.** The `standard` mode
  of Pyright accepts a function without annotations, and ruff runs no `ANN`
  rules and no `D1xx` rules. `CONTRIBUTING.md` asks for typed public APIs with
  Google-style docstrings. So code review checks that rule.
- **The author formats code by hand.** A `ruff format` run rewrites most of the
  repository. A small change then disappears among unrelated diff lines.
- **A suppression names its rule and its reason.** Nearly every `noqa` comment
  in the repository reads `# noqa: <CODE> - <reason>`. An example is
  `# noqa: DTZ001 - invalid input` on a test that gives a naive `datetime` on
  purpose. The exceptions are the bare `# noqa: DTZ001` comments in
  `examples/lamps/graphiti/tests/test_graphiti_lamp.py` and
  `examples/lamps/hindsight/tests/test_hindsight_lamp.py`. All `noqa` comments
  are in tests and benchmarks. The package has none.
- **`./lib/scripts/ruff_report.sh`** writes the grouped output to
  `ruff_report.txt`. `.gitignore` keeps that file out of the repository.

## Language level

- **The code supports Python 3.12 and 3.13.** `requires-python =
  ">=3.12,<3.14"` sets this.
- **Built-in generics and `|` unions:** `dict[str, Any]`, `str | None`,
  `Mapping[str, int]`.
- **The code uses these 3.11 features:** `Self` for methods that return their
  own class, `StrEnum` for `HookOperation`, `HandleCapability`, `HookOutcome`,
  `StructureKind`, and `StructureAction`, and `datetime.UTC` for timestamps.
- **Each timestamp is timezone-aware UTC.** The code makes it with `utc_now()`
  in `genie.memory.block`, not with `datetime.now()`.
- **The core has no third-party runtime imports.** This is a hard rule from
  `CONTRIBUTING.md`. `lib/benchmarks/` also obeys it. Driver imports are in the
  adapters, and they stay optional.

## Imports

- **`from __future__ import annotations` is the first import** in each module,
  except the `__init__.py` modules, `memory/errors.py`, and `query/scope.py`.
  These modules have no annotation that needs it.
- **Internal imports are absolute.** Each one has the form
  `from genie.<area> import <name>`. The package has no relative imports. So a
  reader sees the dependencies of a module and does not need to know where the
  file is.
- **An import of several names uses parentheses and one name on each line.**
  This is the shape that the import sort of ruff makes.
- **Type-only imports go inside `if TYPE_CHECKING:`.** Most of them are in
  adapters. These adapters use the structure record types in annotations. A
  function that builds a record imports the record type inside the function.
- **The adapter imports an optional driver inside the function that needs it,**
  not at module scope. So an import of the adapter never fails because of a
  missing package. `CONTRIBUTING.md` makes this a hard rule: a missing driver
  raises `UnsupportedOperation`.

## Package surface

- **Each module starts with a docstring.** A large adapter uses labelled
  paragraphs (`Storage layout:`, `Writes:`, `Reads:`, `Search:`). They explain
  its strategy one time at the top, so the methods do not repeat it.
- **`__init__.py` re-exports names.** It contains a docstring, the imports,
  and `__all__`. The current `__all__` lists are sorted, with constants such
  as `MAX_NESTING_DEPTH` first. `ruff.toml` does not turn on a rule that
  checks the order.
  - **Docstring:** one line in each subpackage. The root `genie/__init__.py`
    docstring is longer. It summarizes the library and the 2 import styles.
  - **Lazy imports:** `memory/__init__.py` and `policy/__init__.py` also
    define a module `__getattr__`. It imports `Memory`, `MemoryHandle`,
    `AccessDenied`, `Operation`, and `PolicyEngine` on first use, so that
    there is no import cycle.
- **Each public name is also in the root `__all__`.**
  `lib/tests/test_public_imports.py` fails if a subpackage exports a name that
  the root `__all__` does not contain. A caller can import a name as
  `from genie import X` and as `from genie.<area> import X`.
- **A leading underscore marks private.** `memory/_validation.py`,
  `structures/_hooks.py`, and `structures/_routing.py` are internal. The
  package does not re-export them.

## Types and data shapes

| Pattern | Used for |
| --- | --- |
| `@dataclass(frozen=True, slots=True)` | each record that Genie stores or returns |
| `Protocol` with `@runtime_checkable` | each backend, access, and codec contract |
| `__slots__` on a plain class | facades and other objects on frequent code paths |
| Keyword-only arguments | optional parameters on public methods |
| `TypeAlias` | shared vocabulary, for example `BlockRef` and `LinkDirection` |

- **Records are frozen and slotted.** `MemoryBlock`, `MemoryLink`,
  `SearchResult`, `KeyBinding`, and the other records are immutable. A change
  makes a copy: `MemoryBlock.with_updates(**changes)` wraps
  `dataclasses.replace`.
- **Contracts are protocols, not base classes.** An adapter satisfies
  `MemoryBackend` structurally. `@runtime_checkable` lets a test assert
  `isinstance(backend, KeyedStore)` without an import cycle.
- **A protocol method body is `...`,** after its docstring and the blank line.
- **Optional public parameters are keyword-only.** `Memory.put` takes all
  arguments after `self` by keyword. So a new parameter never changes the
  meaning of a positional call.
- **A `TypeAlias` has its own docstring** on the next line, so the alias
  appears in generated documentation.

## Docstrings

- **Docstrings use Google style,** with `Args:`, `Returns:`, `Raises:`, and
  `Attributes:` sections. `CONTRIBUTING.md` requires this style for public
  APIs. `Raises:` lists the exceptions that a caller handles.
- **A blank line follows the docstring,** before the first statement. The code
  base does this in all places.
- **All of the public surface has docstrings:** each module, each public class,
  and each public function and method. The only definitions without a
  docstring are nested closures inside functions.
- **A one-line summary comes first,** then a blank line, and then the details.
- **Exact code names stay in backticks** inside docstrings. The repository uses
  the same rule in its markdown: `` `None` ``, `` `expected_version` ``,
  `` `MemoryPolicy.owner_private()` ``.
- **Docstrings use plain English with high density.** `CONTRIBUTING.md` asks
  for plain English in docs, docstrings, and comments. Sentences are short,
  words are everyday words, and there is no introductory filler. A docstring
  explains what a name means and keeps the name itself.

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

- **Each file has as many comments as its code needs.** The repository varies
  widely, and that is correct. `RedisBackend` has many comments, because its
  Lua scripts, its retry loop, and its key layout are not self-evident.
  `SQLiteBackend` has almost none, because its SQL is easy to read. Neither one
  is a model to copy.
- **Docstrings and comments have different purposes.** A docstring is the
  contract for a caller who does not read the body. A comment is for the reader
  of the body. A function with both is normal, and it is not
  duplication.
- **A comment is most useful for what the code cannot show.** Examples are the
  reason for the order of a check, the meaning of a driver reply, or the
  invariant that makes a branch unreachable:
  `# A dropped socket arrives as an OperationalError with no SQLSTATE.`
- **Embedded non-Python code has a comment block.** A comment comes before each
  Lua script in `RedisBackend`. The comment names its `KEYS` and `ARGV`, its
  return value, and the Python method that changes together with it.
- **The large adapters divide their class with section banners.** A banner is
  a dashed ruler and a section name. `PostgresBackend`, `Neo4jBackend`,
  `MongoDBBackend`, and `RedisBackend` use the same section order: core block
  storage and ownership traversal, keyed collection, ordered event log,
  temporal collection and index, typed links and lineage, portable search, and
  then the private helpers. With the same order, a reader can compare 2
  adapters section by section.

```python
    # ------------------------------------------------------------------
    # Core block storage and ownership traversal
```

## Errors and validation

- **Public code validates arguments at the boundary.** Public constructors and
  methods check their inputs before they do any work. They raise `TypeError`
  for the wrong type and `ValueError` for the wrong value.
- **A message is a full sentence that starts with the parameter name** and ends
  with a period: `"url must be a string."`, `"content_type must not be empty."`.
  Nearly all literal messages in the package follow this form. The tests match
  the message text.
- **Genie exceptions are subclasses of `GenieError`.** The subclasses are
  `MemoryNotFound`, `MemoryConflict`, `UnsupportedOperation`,
  `HookRecursionError`, and the `BackendError` family. `BackendError` has a
  `retryable` attribute, and its `Attributes:` section documents it.
- **The code chains the original failure** with `raise ... from error`, so the
  driver traceback stays available.
- **A missing driver raises `UnsupportedOperation` with the command that installs the extra:**
  `"RedisBackend requires the optional 'redis' package. In the Genie clone, run: python -m pip install -e '.[redis]'"`

## Naming

| Kind | Convention | Examples |
| --- | --- | --- |
| Storage capability protocol | `<Thing>Store` or `<Thing>Index` | `BlockStore`, `KeyedStore`, `SearchIndex`, `TraversalIndex` |
| Access protocol for facades | `<Facade>Access`, or `<Role>Reader` / `<Role>Writer` | `OwnershipAccess`, `GraphAccess`, `BlockReader`, `LinkWriter` |
| Structure facade | A plain noun for the shape that it shows | `OwnershipTree`, `KeyedCollection`, `EventLog` |
| Backend adapter | `<Store>Backend` | `SQLiteBackend`, `MongoDBBackend`, `RoutedBackend` |
| Private module constant | `_UPPER_SNAKE` | `_SCHEMA_VERSION`, `_MAX_BACKOFF_SECONDS` |
| Private helper | `_lower_snake` | `_block_id`, `_copy_metadata` |
| Test stand-in class | `_Fake<Thing>` | `_FakeWatchError`, `_FakeClientModule` |
| Test spy hook | `Recording<Thing>Hook` | `RecordingDenialHook`, `RecordingSinkHook` |
| Environment variable | `GENIE_<AREA>_<SETTING>` | `GENIE_REDIS_URL`, `GENIE_POSTGRES_DSN` |

- **`BUILTIN_OPERATION_NAMES`, `OPERATOR_ALIASES`, and `MAX_NESTING_DEPTH` are
  the only public module constants.** All other names that look like constants
  are private.

## Tests

- **Most tests are plain functions.** The tests use classes only where a class
  is useful: the reusable contract suites in
  `lib/tests/backends/test_backend_contracts.py`, the subclass for each adapter
  that runs them, and the stand-in helpers that a test needs.
- **Setup is usually inline.** The repository has no `conftest.py` and few
  fixtures. So a test builds what it needs in its own body, often with a bare
  `Memory()`. A fixture is a good choice if the setup is really shared or
  expensive.
- **The test name is a sentence about behavior,** for example
  `test_delete_authorizes_the_target_and_not_its_descendants`.
- **A test usually has a one-line docstring** that states the behavior.
- **`pytest.mark.parametrize` takes a tuple of names,** mostly for tables of
  rejected constructor arguments and their expected messages.
- **Live adapter tests have 2 gates:** the driver is importable, and the
  `GENIE_*` connection variables are set:

```python
_LIVE_DRIVER_AVAILABLE = importlib.util.find_spec("psycopg") is not None
_LIVE_REASON = "set GENIE_POSTGRES_DSN and install the postgres extra to run live PostgreSQL tests"
```

- **So a plain `pytest` run is partial,** and a report of that run says so.
  `./lib/scripts/test_full.sh` is the complete run.
- **Tests stay deterministic.** This is another hard rule from
  `CONTRIBUTING.md`. Tests do not depend on a clock, or on software that the
  machine has by chance. Only the gated live tests use a network.

## Examples

- **The module docstring gives the run command,** indented as a code block:

```python
"""Create, read, update, traverse, and recursively delete memory blocks.

Run from the repository root:

    python examples/basic/crud_blocks.py
"""
```

- **Single-file examples have the bootstrap preamble** that puts the repository
  on `sys.path`. So they run from a checkout with nothing installed. Each file
  in `basic/` and `advanced/` has it, and the `from genie import ...` line is
  below it. `lib/benchmarks/run.py` does the same for the `benchmarks` package.
  These files are the only place where a module-level import is not at the
  top. Only `lib/benchmarks/run.py` has a comment that tells why its import
  cannot move.
- **Lamp demos do not have the preamble.** They import
  `examples.lamps.<name>....` directly, because they run as modules with
  `python -m`.
- **The body is one `main()` with a docstring.** An
  `if __name__ == "__main__": main()` guard calls it.
- **`print` shows the result, and `assert` proves it.** The printed lines are
  the tested output. The assertions state the guarantee that the example shows.
- **Output stays the same from run to run.** `lib/tests/test_examples.py`
  compares all of the standard output with `examples/expected/`, so a change in
  the output fails the test.

## Scripts and the benchmark harness

- **Shell scripts use bash with `set -euo pipefail`.** Each script has a comment
  block at the top. The block tells what the script does and which `GENIE_*`
  variables change its behavior. Each script also has a `cd` to the repository
  root, computed from `BASH_SOURCE`.
- **A script with a long run time prints live progress.** A silent gap looks
  like a hang.
- **`lib/benchmarks/` uses the standard library only,** like the core package.
  Its entry point lists its command lines in the module docstring. It also
  gives the meaning of the exit status.

## Related reading

- [Repository organization](07-repo-organization.md) — where a new file goes
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, the checks to run, and the boundaries for a change
- [Detailed architecture](04-detailed-architecture.md) — the invariants that explain some of these conventions
- [Overview](01-overview.md) — the design philosophy that the style supports
