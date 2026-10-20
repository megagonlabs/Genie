"""The stores that the benchmarks measure.

A target is one store, and the function that opens and disposes of it. Each
measured variant gets a new instance. So the writes of one variant cannot
change the numbers of the next variant.

- **2 targets always run.** `in_memory` needs nothing. `sqlite` uses the driver
  that comes with Python.
- **4 server targets run if their driver is installed and their variables are
  set.** They read the same `GENIE_*` variables as the live adapter tests, so
  one environment works for both. A target that cannot run reports a reason
  and no number. The report never counts it as a pass. The module does not
  check if the server is reachable. If the server is not reachable, the open
  of the store fails during the run.
- **A server store resets through the public backend protocol,** not through
  adapter internals. A delete of each root block also deletes its subtree, and
  the links, key bindings, sequence entries, and temporal entries that the
  adapter owns for those blocks. So one loop empties each adapter, current or
  new.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genie import (
    InMemoryBackend,
    MemoryQuery,
    MongoDBBackend,
    Neo4jBackend,
    PostgresBackend,
    RedisBackend,
    Scope,
    SQLiteBackend,
)

SQLITE_DURABILITY = "full"
"""The SQLite synchronous mode for this target. It is the safe default for
production."""

CLEAR_PAGE = 500
"""The blocks that one pass examines when it empties a server store."""

CLEAR_PASS_LIMIT = 10_000
"""The maximum number of reset passes, so an unexpected store cannot make a
run hang."""

BENCHMARK_NAMESPACE = "genie_benchmark"
"""The default PostgreSQL schema and MongoDB database name. It is different
from the test namespace, so a benchmark run cannot empty a store that the live
tests use."""

BENCHMARK_KEY_PREFIX = "genie-benchmark"
"""The default Redis key prefix. It is different from the test prefix for the
same reason."""


@dataclass(frozen=True, slots=True)
class BackendTarget:
    """One store that the benchmarks can use.

    Attributes:
        name: The short name in reports and on the command line.
        storage: A one-line description of what the target stores, and where.
        opener: Returns a new backend and the function that disposes of it.
        skip_reason: The reason that the target cannot run, or `None` if it
            can run.
    """

    name: str
    storage: str
    opener: Callable[[], tuple[Any, Callable[[], None]]]
    skip_reason: str | None = None

    @property
    def available(self) -> bool:
        """`True` if the target can run in this environment."""

        return self.skip_reason is None


def clear_store(backend: Any) -> None:
    """Empty a store with only the public backend protocol.

    A delete of a root block removes the block, each descendant, and the
    dependent state that the adapter owns for them. The function repeats this
    until a global search finds nothing. Then the store is empty.

    Args:
        backend: The backend to empty.

    Raises:
        RuntimeError: If the store still returns blocks after
            `CLEAR_PASS_LIMIT` passes. This means that the deletes do not
            progress, and a benchmark run would measure stale data.
    """

    for _ in range(CLEAR_PASS_LIMIT):
        results = backend.search(MemoryQuery(scope=Scope.GLOBAL, limit=CLEAR_PAGE))
        if not results:
            return
        roots = [result.block.id for result in results if result.block.parent_id is None]
        # Each ownership chain ends at a root, so `roots` is normally not empty.
        # If it is empty, the loop deletes the first block. So a partial page
        # still progresses, and the loop does not get the same page again and
        # again.
        for block_id in roots or [results[0].block.id]:
            backend.delete_block(block_id)
    raise RuntimeError("Could not empty the benchmark store; deletion made no progress.")


def _open_in_memory() -> tuple[Any, Callable[[], None]]:
    """Open a new in-memory backend. It needs no disposal."""

    return InMemoryBackend(), lambda: None


def _open_sqlite() -> tuple[Any, Callable[[], None]]:
    """Open a new SQLite database on a file in a temporary directory."""

    directory = tempfile.mkdtemp(prefix="genie-benchmark-")
    backend = SQLiteBackend(Path(directory) / "memory.db", durability=SQLITE_DURABILITY)

    def dispose() -> None:
        backend.close()
        shutil.rmtree(directory, ignore_errors=True)

    return backend, dispose


def _missing(*names: str) -> list[str]:
    """Return the environment variables in `names` that are not set."""

    return [name for name in names if not os.environ.get(name)]


def _skip_reason(
    *,
    driver: str,
    extra: str,
    required: Sequence[str],
    paired: Sequence[str] = (),
) -> str | None:
    """Give the reason that a server target cannot run, or `None` if it can run.

    Args:
        driver: The import name of the driver package that the adapter needs.
        extra: The name of the optional dependency that installs that driver.
        required: The environment variables that the target needs. All of
            them are necessary.
        paired: The environment variables that go together. The target needs
            all of them or none of them.

    Returns:
        A reason that names exactly what is not available, or `None`.
    """

    reasons = []
    if importlib.util.find_spec(driver) is None:
        reasons.append(f"install the {extra} extra")
    missing_required = _missing(*required)
    if missing_required:
        reasons.append(f"set {', '.join(missing_required)}")
    missing_paired = _missing(*paired)
    if paired and missing_paired and len(missing_paired) != len(paired):
        reasons.append(f"set {', '.join(missing_paired)} too, or none of {', '.join(paired)}")
    return " and ".join(reasons) if reasons else None


def _server_opener(build: Callable[[], Any]) -> Callable[[], tuple[Any, Callable[[], None]]]:
    """Wrap an adapter constructor, so each variant starts with an empty store.

    - **Why:** the harness cannot discard a server between variants as it
      discards a temporary file.
    - **On open:** the opener empties the store. So an interrupted run cannot
      leave stale blocks for the next run.
    - **On close:** the disposal function empties the store again, and then
      closes the backend.

    Args:
        build: Builds the adapter.

    Returns:
        An opener that returns the backend and its disposal function.
    """

    def opener() -> tuple[Any, Callable[[], None]]:
        backend = build()
        try:
            clear_store(backend)
        except Exception:
            _close(backend)
            raise

        def dispose() -> None:
            try:
                clear_store(backend)
            finally:
                _close(backend)

        return backend, dispose

    return opener


def _close(backend: Any) -> None:
    """Close a backend that has a `close` method. Ignore a backend that does not have one."""

    close = getattr(backend, "close", None)
    if callable(close):
        close()


def _neo4j_auth() -> tuple[str, str] | None:
    """Return the Neo4j credentials if both variables are set."""

    user = os.environ.get("GENIE_NEO4J_USER")
    password = os.environ.get("GENIE_NEO4J_PASSWORD")
    if user and password:
        return user, password
    return None


def _redis_auth() -> tuple[str, str] | None:
    """Return the Redis credentials if both variables are set."""

    user = os.environ.get("GENIE_REDIS_USER")
    password = os.environ.get("GENIE_REDIS_PASSWORD")
    if user and password:
        return user, password
    return None


def server_targets() -> tuple[BackendTarget, ...]:
    """Build the 4 server-backed targets, available or not available.

    - **Variables:** each target reads the same `GENIE_*` variables as the live
      adapter tests.
    - **Separate namespace:** the PostgreSQL schema, the MongoDB database, and
      the Redis key prefix have a default namespace for benchmarks only. So a
      benchmark run does not empty those stores of the tests.
    - **Neo4j exception:** the default Neo4j database is `neo4j`, which is
      also the default of the live tests.

    Returns:
        The 4 targets. A target that cannot run has a `skip_reason`.
    """

    postgres_dsn = os.environ.get("GENIE_POSTGRES_DSN", "")
    mongodb_uri = os.environ.get("GENIE_MONGODB_URI", "")
    neo4j_uri = os.environ.get("GENIE_NEO4J_URI", "")
    redis_url = os.environ.get("GENIE_REDIS_URL", "")
    postgres_schema = os.environ.get("GENIE_POSTGRES_SCHEMA", BENCHMARK_NAMESPACE)
    mongodb_database = os.environ.get("GENIE_MONGODB_DATABASE", BENCHMARK_NAMESPACE)
    neo4j_database = os.environ.get("GENIE_NEO4J_DATABASE", "neo4j")
    redis_prefix = os.environ.get("GENIE_REDIS_KEY_PREFIX", BENCHMARK_KEY_PREFIX)

    return (
        BackendTarget(
            name="postgres",
            storage=f"PostgresBackend on a server, schema {postgres_schema!r}, durability='full'",
            opener=_server_opener(
                lambda: PostgresBackend(postgres_dsn, schema=postgres_schema)
            ),
            skip_reason=_skip_reason(
                driver="psycopg",
                extra="postgres",
                required=("GENIE_POSTGRES_DSN",),
            ),
        ),
        BackendTarget(
            name="mongodb",
            storage=f"MongoDBBackend on a replica set, database {mongodb_database!r}, durability='full'",
            opener=_server_opener(
                lambda: MongoDBBackend(mongodb_uri, database=mongodb_database)
            ),
            skip_reason=_skip_reason(
                driver="pymongo",
                extra="mongodb",
                required=("GENIE_MONGODB_URI",),
            ),
        ),
        BackendTarget(
            name="neo4j",
            storage=f"Neo4jBackend on a server, database {neo4j_database!r}",
            opener=_server_opener(
                lambda: Neo4jBackend(neo4j_uri, auth=_neo4j_auth(), database=neo4j_database)
            ),
            skip_reason=_skip_reason(
                driver="neo4j",
                extra="neo4j",
                required=("GENIE_NEO4J_URI",),
                paired=("GENIE_NEO4J_USER", "GENIE_NEO4J_PASSWORD"),
            ),
        ),
        BackendTarget(
            name="redis",
            storage=f"RedisBackend on a server, key prefix {redis_prefix!r}",
            opener=_server_opener(
                lambda: RedisBackend(redis_url, auth=_redis_auth(), key_prefix=redis_prefix)
            ),
            skip_reason=_skip_reason(
                driver="redis",
                extra="redis",
                required=("GENIE_REDIS_URL",),
                paired=("GENIE_REDIS_USER", "GENIE_REDIS_PASSWORD"),
            ),
        ),
    )


def default_targets() -> tuple[BackendTarget, ...]:
    """Return all targets that a run knows, also the skipped targets.

    Returns:
        The 2 local targets first, then the 4 server targets. A server target
        without its driver or its variables has a `skip_reason`. The report
        shows it as skipped and does not remove it. So a report always tells
        what it did not measure.
    """

    return (
        BackendTarget(
            name="in_memory",
            storage="InMemoryBackend, one process, nothing persisted",
            opener=_open_in_memory,
        ),
        BackendTarget(
            name="sqlite",
            storage=f"SQLiteBackend on a file in a throwaway directory, durability={SQLITE_DURABILITY!r}",
            opener=_open_sqlite,
        ),
        *server_targets(),
    )
