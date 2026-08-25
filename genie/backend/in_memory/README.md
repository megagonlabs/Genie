# In-memory backend

`reference.py` provides `InMemoryBackend`, Genie's reference implementation. It
needs no extra packages. It keeps blocks, links, keys, logs, and temporal
entries in Python dictionaries, and a reentrant lock protects structural
operations inside one process.

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
```

This backend implements the core contract and every optional structure
capability. Its storage is predictable, and its code is working guidance for
tests, examples, prototypes, and people writing their own adapters.

All state is lost when the process exits. There is no durable storage, no
coordination between processes, no backup, and no operational monitoring. So it
is not a backend for a persistent deployment.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
