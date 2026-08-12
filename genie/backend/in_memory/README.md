# In-memory backend

`reference.py` provides `InMemoryBackend`, Genie's dependency-free reference
implementation. It stores blocks, links, keys, logs, and temporal entries in
Python dictionaries; a reentrant lock protects structural operations within
one process.

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
```

The backend implements the core contract and every optional structure
capability. It provides deterministic storage and executable guidance for
tests, examples, prototypes, and adapter authors.

State disappears when the process exits. It has no durable storage,
cross-process coordination, backup, or operational monitoring, so it is not a
persistent deployment backend.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
