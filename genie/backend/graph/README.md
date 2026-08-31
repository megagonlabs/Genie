# Neo4j backend

`Neo4jBackend` is a persistent graph adapter built directly on the official
Neo4j Python driver. It implements the core backend contract and every optional
structure capability.

```bash
pip install "genie[neo4j]"
```

```python
from genie import Memory, Neo4jBackend


with Neo4jBackend(
    "neo4j://localhost:7687",
    auth=("neo4j", "password"),
) as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

## Behavior

- **Blocks are graph nodes.** Ownership and typed links are relationships. Link, key, log, and temporal records also get stable graph identities.
- **Portable encodings** are used for values, metadata, policies, namespaces, and timestamps, instead of Neo4j-specific value types.
- **Atomicity matches SQLite:** managed transactions, uniqueness constraints, stable sequence counters, and one database mutation guard. That includes compare-and-swap updates, conditional subtree deletion, and DAG insertion.
- **A shared bookmark manager** keeps read-after-write ordering when operations use different sessions in a cluster.
- **Construction checks the schema:** identity, version, constraint type, index type, entity type, and index state. Versioned migrations are idempotent, so running one again is safe and schema statements (DDL) that were interrupted can be resumed. `initialize_schema=False` validates the schema without running DDL.
- **Driver failures map to Genie's stable `BackendError` types.** The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared driver.

**Permissions needed:**

- **The schema initializer** needs permission to create and inspect constraints and indexes.
- **Runtime identities** need normal access to Genie nodes and relationships, plus permission to inspect the existing schema.

## Concurrency and limits

- **Writes run one at a time** through a single `GenieGuard` node. This favors correctness and matches the one-writer behavior of SQLite, but it does limit write throughput.
- **A commit whose reply never arrives is retried, and the retry can repeat work.** Writes run inside a managed transaction, which retries a transaction that fails with a transient or connection error. When the connection dies during the commit, the driver cannot know whether the server applied it, so the retry re-runs the whole transaction function.
- **Most writes then raise `MemoryConflict` on the repeat,** because they check for their own result first: a block id, a link id, a key address, or a temporal entry that already exists, and any `expected_version` or `expected_block_id` write whose expected value has moved. The write did land, but the caller is told it conflicted.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key, so a repeat appends the same block a second time at a new sequence, and the caller receives the second entry. `RedisBackend` and `MongoDBBackend` carry the same limit for the same reason, because they retry a lost commit reply too. An application that needs exactly-once appends should carry its own token in the block and skip a duplicate on read.
- **Write through `Neo4jBackend`.** Genie labels, relationship types, constraints, and indexes belong to the backend, and writing to the database directly can skip the reference, temporal, and DAG checks.
- **Portable search scans the candidates inside the scope** and then applies Genie's normal matcher. Use a separate search implementation when you need large full-text or vector indexes.
- **Deployment still defines** server replication, backup, monitoring, capacity, TLS, and credential policies.

## Tests

- **Full suite:** run `../../../scripts/test_full.sh` from the repository root to start a throwaway Neo4j container and run everything.
- **Your own server:** live tests need `GENIE_NEO4J_URI`.
- **Optional variables:** `GENIE_NEO4J_USER`, `GENIE_NEO4J_PASSWORD`, and `GENIE_NEO4J_DATABASE` choose the authentication and the target database.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [document
backends](../document/README.md), and [Neo4j
tests](../../../tests/backends/test_neo4j_backend.py).
