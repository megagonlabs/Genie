# Lamps

Lamps are focused, runnable examples that show how Genie building blocks can be
composed into application-level memory patterns and systems. A lamp may explore
an original design or demonstrate a subset of another system's behavior. Lamps
are examples, not compatibility layers or production implementations.

Each lamp lives under `lamps/<name>`. Its files can follow whatever structure
best fits the example: a small lamp may be one script, while a larger lamp may
use a package with several modules. In either case, a lamp should:

- Explain its purpose, concept mapping, and important limits.
- Keep application-specific behavior outside the Genie core library.
- Be runnable from the repository root.
- Keep its tests under `lamps/<name>/tests` rather than the top-level `tests`
  directory.
- Avoid unnecessary dependencies, hidden state, and nondeterministic behavior.

Pytest discovers both the core test suite and co-located lamp tests through the
configuration in `pyproject.toml`.
