# Neo4j backend

`Neo4jBackend` is a persistent graph adapter that uses the official Neo4j
Python driver directly. It implements the core backend contract, all
optional structure capabilities, and `RouteCatalog`.

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

- **`"password"` is a placeholder.** Your own Neo4j password goes in its place. Neo4j asks for a new password the first time a server starts, so no default password works here. The URI and the `neo4j` user name are real defaults.
- **No driver:** without the `neo4j` package, the constructor raises `UnsupportedOperation`.

## Behavior

- **Blocks are graph nodes.** Ownership is a relationship. A typed link is a link node and also a direct relationship between its 2 blocks. Link, key, log, and temporal records are nodes with stable identities.
- **Route catalog entries are `GenieRouteEntry` nodes.** The `genie_route_entry_block_id_unique` constraint keeps one node for each `block_id`. An entry has no relationship to a block node, and block writes and deletes do not change it. So the database of a route can also hold the catalog of a `RoutedBackend`.
- **Portable encodings:** values, metadata, policies, namespaces, and timestamps use portable encodings, not Neo4j-specific value types.
- **Atomicity matches SQLite:** managed transactions, uniqueness constraints, stable sequence counters, and one database mutation guard. This includes compare-and-swap updates, conditional subtree delete, and DAG insert.
- **A shared bookmark manager** keeps the read-after-write order when operations use different sessions in a cluster.
- **Construction checks the schema:** identity, version, constraint type, index type, entity type, and index state. Versioned migrations are idempotent. So a second run is safe, and schema statements (DDL) that stopped before the end can continue. `initialize_schema=False` checks the schema and runs no DDL.
- **Driver failures map to stable Genie error types.** Most become a `BackendError` subclass, and a constraint violation becomes `MemoryConflict`. The original error stays available as `__cause__`.
- **`close()` waits for active operations** before it closes the shared driver.

**Permissions needed:**

- **The schema initializer** needs permission to create and examine constraints and indexes.
- **Runtime identities** need normal access to Genie nodes and relationships, and permission to examine the current schema.

## Concurrency and limits

- **Writes run one at a time** through a single `GenieGuard` node. This puts correctness first and matches the one-writer behavior of SQLite, but it limits the write throughput.
- **A commit with a lost reply runs again, and the second run can repeat work.** Writes run inside a managed transaction. A managed transaction runs again after a transient error or a connection error. When the connection stops during the commit, the driver cannot know whether the server applied it. So the retry runs the whole transaction function again.
- **Most writes then raise `MemoryConflict` on the repeat,** because they first check for their own result: a block id, a link id, a key address, a temporal entry, or a route catalog entry that already exists, and any `expected_version` or `expected_block_id` write whose expected value has changed. The write did happen, but the caller gets a conflict.
- **A repeated `claim_route` keeps its entry.** The first run stored the entry, so the caller gets `MemoryConflict`, and the block id stays claimed with no block. `RoutedBackend` then stores no block for that id, and a later put of the id raises `MemoryConflict`. `release_routes` removes the entry. A repeated `release_routes` is safe, because it ignores an id with no entry.
- **`append_log_entry` is the exception.** It allocates the next sequence with no uniqueness key. So a repeat appends the same block a second time at a new sequence, and the caller gets the second entry. `RedisBackend` and `MongoDBBackend` have the same limit for the same reason, because they also retry a lost commit reply. An application that needs exactly-once appends keeps its own token in the block, and it skips a duplicate on read.
- **Writes go through `Neo4jBackend`.** Genie labels, relationship types, constraints, and indexes belong to the backend. A direct write to the database can skip the reference, temporal, and DAG checks.
- **Portable search scans the candidates inside the scope,** and then applies the normal matcher of Genie. Large full-text or vector indexes need a separate search implementation.
- **Deployment still defines** server replication, backup, monitoring, capacity, TLS, and credential policies.

## Tests

- **Full suite:** `lib/scripts/test_full.sh`, run from the repository root, starts disposable server containers, Neo4j among them, and runs the complete suite.
- **Your own server:** live tests need `GENIE_NEO4J_URI`.
- **Optional variables:** `GENIE_NEO4J_USER`, `GENIE_NEO4J_PASSWORD`, and `GENIE_NEO4J_DATABASE` select the authentication and the target database. `GENIE_NEO4J_USER` and `GENIE_NEO4J_PASSWORD` go together, and `GENIE_NEO4J_DATABASE` defaults to `neo4j`.

Related: [Backend contracts](../README.md), [relational
backends](../relational/README.md), [document
backends](../document/README.md), and [Neo4j
tests](../../../../tests/backends/test_neo4j_backend.py).
