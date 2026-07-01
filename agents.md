## Python Environment Management

Always run Python commands through the project Conda environment. Do not use
bare `python`, `pytest`, or `pip` commands directly.

Use:

```bash
conda run -n "genie" python file_name.py
conda run -n "genie" python -m pytest
conda run -n "genie" python -m compileall -q genie tests examples
```

## Library Purpose

Genie is a utility library for developers building their own memory systems
around memory blocks, parent/child containment, and scoped access. Its core
responsibility is to enforce the block model and containment semantics while
remaining platform-agnostic, storage/database-agnostic, use-case-agnostic, and
content value-type agnostic.

Keep application business logic, memory taxonomies, agent behavior, storage
topology, retrieval strategy, and domain-specific value semantics outside the
core library. Parent/child relationships are for containment and scoped
operations; links and mounts are for non-containment relationships. Changes
should strengthen Genie's structural primitives, extension contracts, examples,
and documentation without turning Genie into an opinionated agent framework or
memory product.

## Engineering Standards

Use judgment to keep changes aligned with best industry standards and practices
for a developer-facing Python library. Apply these standards proportionally to
the size, risk, and user-facing impact of the change:

- Prefer focused, minimal changes that fit the existing architecture.
- Preserve Genie's platform-agnostic, storage/database-agnostic,
  use-case-agnostic, and content-agnostic design; avoid coupling core APIs to
  one agent framework, storage engine, data model, retrieval strategy, memory
  workflow, or value type.
- Keep public APIs clear and typed, with Google-style docstrings and predictable
  exceptions where they affect users.
- Prefer explicit, least-privilege behavior for scoped memory access and policy
  checks.
- Add or update tests when behavior, public APIs, edge cases, or regressions are
  affected.
- Update examples and documentation when user-facing behavior changes.
- Avoid hidden dependencies, unnecessary global state, and non-deterministic
  behavior in tests or examples.
- Keep examples runnable from the repository root and useful for first-time
  developers when examples are touched.
- Avoid broad refactors, dependency additions, or compatibility breaks unless
  they are directly required and documented.
