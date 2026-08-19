## Python Environment Management

Always run Python commands through the project Conda environment. Do not use
bare `python`, `pytest`, or `pip` commands directly.

Use:

```bash
conda run -n "genie" python file_name.py
conda run -n "genie" python -m pytest
conda run -n "genie" python -m compileall -q genie tests examples lamps
```

## Library Purpose

Genie is a utility library for developers building their own memory systems
from typed memory blocks, parent/child ownership, typed relationships, scoped
access, and optional composable structures. It also defines extension boundaries
for backends, search, policies, hooks, and codecs. Genie should stay independent
of agent frameworks, concrete databases and search engines, memory use cases,
and stored value types.

Keep application business logic, memory taxonomies, agent behavior, storage
layout, retrieval strategy, and domain-specific value meaning outside the core
library. Parent/child relationships are for ownership and scoped operations;
links and mounts are for relationships that do not imply ownership. Structure
facades organize blocks without defining what their values mean. Changes should
improve Genie's memory model, public interfaces, examples, and documentation
without turning Genie into an opinionated agent framework or memory product.

## Application Memory and Agent Behavior

Application-owned memory classes may compose Genie facades, hide infrastructure
setup, and expose value-oriented read and write operations. They should return
stored values and relationships without deciding what an agent should do with
them.

Keep task interpretation, action selection, planning, extraction, consolidation,
ranking policy, and other decision logic in agents or application services. Agent
framework integrations and deterministic agents are appropriate in examples and
lamps, but they should not become dependencies of the core library.

## Structures and Backends

The structure facades are optional and composable. Applications may use one, a
subset, or all of them over the same memory blocks. Reuse existing facades when
building higher-level structures instead of creating duplicate storage models.

Genie is database-agnostic, not database-free. `InMemoryBackend` is the reference
implementation for tests, examples, and local prototypes. Production adapters may
use SQL, graph, vector, document, object, or composed storage while preserving the
public backend contracts.

Facades should depend on narrow backend capability protocols rather than concrete
backend classes. Do not assume that every backend implements every optional
capability. When an operation requires atomic behavior that cannot be implemented
safely as a portable read followed by a write, define or reuse an atomic backend
capability and cover it with backend contract and concurrency tests.

## Engineering Standards

Use judgment to keep changes aligned with best industry standards and practices
for a developer-facing Python library. Apply these standards proportionally to
the size, risk, and user-facing impact of the change:

- Prefer focused, minimal changes that fit the existing architecture.
- Preserve Genie's platform-agnostic, storage/database-agnostic,
  use-case-agnostic, and content-agnostic design; avoid coupling core APIs to
  one agent framework, storage engine, data model, retrieval strategy, memory
  workflow, or value type.
- Keep docs and comments plain. Avoid fancy phrases when a direct and clearer wording/phrase works.
- Keep public APIs clear and typed, with Google-style docstrings and predictable
  exceptions where they affect users.
- Prefer explicit, least-privilege behavior for scoped memory access and policy
  checks.
- Add or update tests when behavior, public APIs, edge cases, or regressions are
  affected.
- Add reusable backend contract and concurrency tests when backend capability
  behavior or atomicity is affected.
- Update examples and documentation when user-facing behavior changes.
- Avoid hidden dependencies, unnecessary global state, and non-deterministic
  behavior in tests or examples.
- Keep examples runnable from the repository root and useful for first-time
  developers when examples are touched.
- Avoid broad refactors, dependency additions, or compatibility breaks unless
  they are directly required and documented.

## Repository Conventions

- Keep core library tests under `tests/`.
- Keep each lamp's tests under `lamps/<name>/tests`.
- Organize small runnable examples under `examples/basic/` or
  `examples/advanced/` as appropriate.
- Treat lamps as architectural demonstrations, not compatibility layers or
  production implementations. Document their concept mappings and deliberate
  limits.
- Run examples and lamps from the repository root through the project Conda
  environment.
