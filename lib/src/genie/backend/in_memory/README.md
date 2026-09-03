# In-memory backend

`reference.py` provides `InMemoryBackend`, Genie's reference implementation.

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
```

- **No extra packages.** It keeps blocks, links, keys, logs, and temporal entries in Python dictionaries.
- **A reentrant lock** protects structural operations inside one process.
- **`contains` walks up from the candidate,** which reads one block per level instead of the whole subtree. `update_block` uses it to reject a move under the block's own descendant, so it is the guard that keeps the ownership tree acyclic.
- **Full contract.** It implements the core contract and every optional structure capability.
- **Working guidance.** Its storage is predictable and its code is a reference for tests, examples, prototypes, and people writing their own adapters.
- **Not for a persistent deployment.** All state is lost when the process exits: no durable storage, no coordination between processes, no backup, and no operational monitoring.

Related: [Backend contracts](../README.md), [Memory](../../memory/README.md),
and [Structures](../../structures/README.md).
