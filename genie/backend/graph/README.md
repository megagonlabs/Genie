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

- Blocks are graph nodes. Ownership and typed links are relationships. Link,
  key, log, and temporal records also get stable graph identities.
- Values, metadata, policies, namespaces, and timestamps use strict portable
  encodings instead of Neo4j-specific value types.
- Managed transactions, uniqueness constraints, stable sequence counters, and
  one database mutation guard give the same atomic behavior as SQLite. That
  includes CAS, conditional subtree deletion, and DAG insertion.
- A shared bookmark manager keeps read-after-write ordering when operations use
  different sessions in a cluster.
- On construction the backend checks the schema identity, version, constraint
  type, index type, entity type, and index state. Versioned migrations are
  idempotent, so DDL that was interrupted can be resumed. Passing
  `initialize_schema=False` validates the schema without running DDL.
- Driver failures are mapped to Genie's stable `BackendError` types. The
  original error stays available as `__cause__`.
- `close()` waits for active operations before it closes the shared driver.

The schema initializer needs permission to create and inspect constraints and
indexes. Runtime identities need normal access to Genie nodes and relationships,
plus permission to inspect the existing schema.

## Concurrency and limits

Writes run one at a time through a single `GenieGuard` node. This favors
correctness and matches the one-writer behavior of SQLite, but it does limit
write throughput.

Genie labels, relationship types, constraints, and indexes belong to the
backend. Write through `Neo4jBackend`. Writing to the database directly can skip
the reference, temporal, and DAG checks.

Portable search scans the candidates inside the scope and then applies Genie's
normal matcher. Use a separate search implementation when you need large
full-text or vector indexes. A deployment still has to define server
replication, backup, monitoring, capacity, TLS, and credential policies.

Run `../../../scripts/test_full.sh` from the repository root to start a
throwaway Neo4j container and run the whole suite. If you run the server
yourself, live tests need `GENIE_NEO4J_URI`. The optional `GENIE_NEO4J_USER`,
`GENIE_NEO4J_PASSWORD`, and `GENIE_NEO4J_DATABASE` variables choose the
authentication and the target database.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), and [Neo4j
tests](../../../tests/test_neo4j_backend.py).
