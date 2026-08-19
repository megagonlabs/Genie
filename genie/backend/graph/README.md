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

- Blocks are graph nodes; ownership and typed links are relationships. Link,
  key, log, and temporal records also have stable graph identities.
- Values, metadata, policies, namespaces, and timestamps use strict portable
  encodings instead of Neo4j-specific application value types.
- Managed transactions, uniqueness constraints, stable sequence counters, and
  one database mutation guard provide the same atomic semantics as SQLite,
  including CAS, conditional subtree deletion, and DAG insertion.
- A shared bookmark manager preserves read-after-write ordering when operations
  use different sessions in a cluster.
- Schema identity, version, constraint type, index type, entity type, and index
  state are checked during construction. Versioned migrations are idempotent so
  interrupted DDL can be resumed. `initialize_schema=False` validates without
  running DDL.
- Driver failures use Genie's stable `BackendError` types. The original error
  remains available as `__cause__`.
- `close()` waits for active operations before closing the shared driver.

The schema initializer needs permission to create and inspect constraints and
indexes. Runtime identities need normal Genie node and relationship access plus
permission to inspect the existing schema.

## Concurrency and limits

Writes are serialized through one `GenieGuard` node. This favors correctness
and matches SQLite's one-writer behavior, but it limits write throughput.

Genie labels, relationship types, constraints, and indexes are backend-owned.
Write through `Neo4jBackend`; direct database writes can bypass reference,
temporal, and DAG checks.

Portable search scans scoped candidates and then uses Genie's normal matcher.
Use a separate search implementation when large full-text or vector indexes are
required. Deployments must still define server replication, backup, monitoring,
capacity, TLS, and credential policies.

Run `../../../scripts/test_full.sh` from the repository root to start a
disposable Neo4j container and execute the complete suite. When managing the
server yourself, live tests require `GENIE_NEO4J_URI`; optional
`GENIE_NEO4J_USER`, `GENIE_NEO4J_PASSWORD`, and `GENIE_NEO4J_DATABASE` select
authentication and the target database.

Related: [Backend contracts](../README.md), [relational backends](../relational/README.md),
and [Neo4j tests](../../../tests/test_neo4j_backend.py).
