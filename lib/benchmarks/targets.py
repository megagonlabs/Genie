"""Stores the benchmarks measure.

A target is one store plus the function that opens and disposes of it. Every
measured variant gets a fresh instance, so nothing one variant writes can move
the next variant's numbers.

- **Two targets always run.** `in_memory` needs nothing and `sqlite` uses the
  driver that ships with Python.
- **Four server targets run when their server is reachable.** They read the
  same `GENIE_*` variables the live adapter tests already use, so one
  environment serves both. A target that cannot run reports a reason instead of
  a number and is never folded into a pass.
- **A server store is reset through the public backend protocol,** not through
  adapter internals. Deleting every root block cascades to its subtree and to
  the links, key bindings, sequence entries, and temporal entries the adapter
  owns for those blocks, so one loop empties any adapter, current or new.
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
"""SQLite synchronous mode used here. The production-safe default."""

CLEAR_PAGE = 500
"""Blocks looked at per pass when emptying a server store."""

CLEAR_PASS_LIMIT = 10_000
"""Safety cap on reset passes, so a surprising store cannot hang a run."""

BENCHMARK_NAMESPACE = "genie_benchmark"
"""Default schema and database name, kept apart from the test namespace so a
benchmark run cannot empty a store the live tests are using."""

BENCHMARK_KEY_PREFIX = "genie-benchmark"
"""Default Redis key prefix, kept apart from the test prefix for the same
reason."""


@dataclass(frozen=True, slots=True)
class BackendTarget:
    """One store the benchmarks can run against.

    Attributes:
        name: Short name used in reports and on the command line.
        storage: One-line description of what the target stores and where.
        opener: Returns a fresh backend and the function that disposes of it.
        skip_reason: Why the target cannot run, or `None` when it can.
    """

    name: str
    storage: str
    opener: Callable[[], tuple[Any, Callable[[], None]]]
    skip_reason: str | None = None

    @property
    def available(self) -> bool:
        """Whether the target can run in this environment."""

        return self.skip_reason is None


def clear_store(backend: Any) -> None:
    """Empty a store using only the public backend protocol.

    Deleting a root block removes the block, every descendant, and the
    dependent state the adapter owns for them. Repeating that until a global
    search finds nothing leaves the store empty.

    Args:
        backend: Backend to empty.

    Raises:
        RuntimeError: If the store still returns blocks after
            `CLEAR_PASS_LIMIT` passes, which means deletion is not making
            progress and a benchmark run would be measuring stale data.
    """

    for _ in range(CLEAR_PASS_LIMIT):
        results = backend.search(MemoryQuery(scope=Scope.GLOBAL, limit=CLEAR_PAGE))
        if not results:
            return
        roots = [result.block.id for result in results if result.block.parent_id is None]
        # Every ownership chain ends at a root, so `roots` is normally not
        # empty. Falling back to the first block keeps a partial page making
        # progress instead of looping.
        for block_id in roots or [results[0].block.id]:
            backend.delete_block(block_id)
    raise RuntimeError("Could not empty the benchmark store; deletion made no progress.")


def _open_in_memory() -> tuple[Any, Callable[[], None]]:
    """Open a fresh in-memory backend that needs no disposal."""

    return InMemoryBackend(), lambda: None


def _open_sqlite() -> tuple[Any, Callable[[], None]]:
    """Open a fresh SQLite database on a file in a throwaway directory."""

    directory = tempfile.mkdtemp(prefix="genie-benchmark-")
    backend = SQLiteBackend(Path(directory) / "memory.db", durability=SQLITE_DURABILITY)

    def dispose() -> None:
        backend.close()
        shutil.rmtree(directory, ignore_errors=True)

    return backend, dispose


def _missing(*names: str) -> list[str]:
    """Return the environment variables among `names` that are not set."""

    return [name for name in names if not os.environ.get(name)]


def _skip_reason(
    *,
    driver: str,
    extra: str,
    required: Sequence[str],
    paired: Sequence[str] = (),
) -> str | None:
    """Explain why a server target cannot run, or return `None` when it can.

    Args:
        driver: Import name of the driver package the adapter needs.
        extra: Name of the optional dependency that installs that driver.
        required: Environment variables that must all be set.
        paired: Environment variables that must be set together or not at all.

    Returns:
        A reason naming exactly what is missing, or `None`.
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
    """Wrap an adapter constructor so each variant starts from an empty store.

    A server cannot be thrown away between variants the way a temporary file
    can, so the store is emptied when it is opened rather than when it is
    closed. Doing it on open also means an interrupted run cannot leave stale
    blocks behind for the next one.

    Args:
        build: Builds the adapter.

    Returns:
        An opener returning the backend and its disposal function.
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
    """Close a backend that has a `close` method, and ignore one that does not."""

    close = getattr(backend, "close", None)
    if callable(close):
        close()


def _neo4j_auth() -> tuple[str, str] | None:
    """Return the Neo4j credentials when both are set."""

    user = os.environ.get("GENIE_NEO4J_USER")
    password = os.environ.get("GENIE_NEO4J_PASSWORD")
    if user and password:
        return user, password
    return None


def _redis_auth() -> tuple[str, str] | None:
    """Return the Redis credentials when both are set."""

    user = os.environ.get("GENIE_REDIS_USER")
    password = os.environ.get("GENIE_REDIS_PASSWORD")
    if user and password:
        return user, password
    return None


def server_targets() -> tuple[BackendTarget, ...]:
    """Build the four server-backed targets, available or not.

    Each one reads the same `GENIE_*` variables the live adapter tests use. The
    schema, database, and key prefix default to a benchmark-only namespace, so
    running the benchmarks never empties a store the tests are using.

    Returns:
        The four targets, each carrying a `skip_reason` when it cannot run.
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
    """Return every target a run knows about, skipped ones included.

    Returns:
        The two local targets first, then the four server targets. A server
        target with no reachable server carries a `skip_reason` and is reported
        as skipped rather than left out, so a report always says what it did
        not measure.
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
