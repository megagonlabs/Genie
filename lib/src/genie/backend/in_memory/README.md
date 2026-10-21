# In-memory backend

`reference.py` gives `InMemoryBackend`, the reference implementation of Genie.

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
```

- **No extra packages.** It keeps blocks, links, keys, logs, and temporal entries in Python dictionaries.
- **A reentrant lock** protects structural operations inside one process.
- **`contains` walks up from the candidate.** It reads one block for each level, not the whole subtree. `update_block` uses it to reject a move of a block under its own descendant. So it is the guard that keeps the ownership tree acyclic.
- **Full contract.** It implements the core contract and all optional structure capabilities.
- **Reference code.** Its storage is predictable, and its code is a reference for tests, examples, prototypes, and authors of their own adapters.
- **Not for a persistent deployment.** All state goes away when the process stops. It has no durable storage, no coordination between processes, no backup, and no operational monitoring.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
