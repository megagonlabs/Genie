"""Unit and opt-in live tests for the psycopg PostgreSQL backend."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, RLock
from typing import Any

import pytest

from genie import (
    AcyclicLinkStore,
    BackendBusy,
    BackendCapacityExceeded,
    BackendCorruption,
    BackendError,
    BackendReadOnly,
    BackendUnavailable,
    KeyedStore,
    Memory,
    MemoryBackend,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    MemoryPolicy,
    MemoryQuery,
    PostgresBackend,
    Scope,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.relational import postgres as postgres_module
from genie.structures import TemporalEntry
from tests.backends.test_backend_contracts import (
    AcyclicLinkStoreContract,
    BlockStoreContract,
    KeyedStoreContract,
    LinkStoreContract,
    SequenceStoreContract,
    TemporalIndexContract,
    TemporalStoreContract,
    TraversalIndexContract,
)


def test_postgres_backend_implements_every_backend_protocol() -> None:
    """The class exposes the complete core and optional capability surface."""

    backend = object.__new__(PostgresBackend)

    assert isinstance(backend, MemoryBackend)
    assert isinstance(backend, AcyclicLinkStore)
    assert isinstance(backend, KeyedStore)
    assert isinstance(backend, SequenceStore)
    assert isinstance(backend, TemporalStore)
    assert isinstance(backend, TemporalIndex)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"conninfo": 1}, TypeError, "conninfo must be a string"),
        ({"conninfo": ""}, ValueError, "conninfo must not be empty"),
        ({"conninfo": "postgresql:///app", "schema": 1}, TypeError, "schema must be a string"),
        ({"conninfo": "postgresql:///app", "schema": ""}, ValueError, "schema must not be empty"),
        ({"conninfo": "postgresql:///app", "schema": "public schema"}, ValueError, "schema must contain only letters"),
        ({"conninfo": "postgresql:///app", "schema": "1genie"}, ValueError, "schema must contain only letters"),
        ({"conninfo": "postgresql:///app", "schema": 'a"; DROP TABLE blocks; --'}, ValueError, "schema must contain only letters"),
        ({"conninfo": "postgresql:///app", "schema": "g" * 64}, ValueError, "schema must be at most 63 bytes"),
        ({"conninfo": "postgresql:///app", "timeout": True}, TypeError, "timeout must be a number"),
        ({"conninfo": "postgresql:///app", "timeout": -1}, ValueError, "timeout must be a finite number"),
        ({"conninfo": "postgresql:///app", "connect_timeout": float("inf")}, ValueError, "connect_timeout must be a finite number"),
        ({"conninfo": "postgresql:///app", "durability": 1}, TypeError, "durability must be a string"),
        ({"conninfo": "postgresql:///app", "durability": "fast"}, ValueError, "durability must be either"),
        ({"conninfo": "postgresql:///app", "verify_server_role": 1}, TypeError, "verify_server_role must be"),
        ({"conninfo": "postgresql:///app", "initialize_schema": 1}, TypeError, "initialize_schema must be"),
    ],
)
def test_postgres_backend_rejects_invalid_configuration_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """Invalid public configuration never attempts a network connection."""

    monkeypatch.setattr(
        postgres_module,
        "_load_psycopg",
        lambda: pytest.fail("driver should not be loaded"),
    )

    with pytest.raises(error, match=message):
        PostgresBackend(**kwargs)


def test_postgres_backend_explains_its_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing driver installations produce an actionable public error."""

    def missing() -> tuple[Any, Any, tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
        raise UnsupportedOperation("install 'genie[postgres]'")

    monkeypatch.setattr(postgres_module, "_load_psycopg", missing)

    with pytest.raises(UnsupportedOperation, match=r"genie\[postgres\]"):
        PostgresBackend("postgresql:///app")


def test_postgres_serialization_is_strict_and_defensive() -> None:
    """The stored column representation preserves JSON value types."""

    original = MemoryBlock(
        id="note",
        type="note",
        value={"items": [1, True, None], "ratio": 0.5},
        metadata={"nested": {"value": "kept"}},
        policy=MemoryPolicy.owner_private(),
        created_by="agent:writer",
    )

    values = postgres_module._block_values(original)
    row = {
        "id": values[0],
        "parent_id": values[1],
        "type": values[2],
        "value_json": values[3],
        "content_type": values[4],
        "metadata_json": values[5],
        "policy_json": values[6],
        "created_at": values[7],
        "updated_at": values[8],
        "created_by": values[9],
        "version": values[10],
    }

    restored = postgres_module._block_from_row(row)

    assert restored == original
    assert restored.value["items"][1] is True
    assert isinstance(restored.value["items"][0], int)
    assert restored.policy == original.policy


def test_postgres_rejects_values_that_are_not_strict_json() -> None:
    """A value that cannot round-trip through JSON is refused on the way in."""

    with pytest.raises(TypeError, match="must be strict JSON data"):
        postgres_module._dump_json({1: "integer key"}, field_name="block value")
    with pytest.raises(TypeError, match="must be strict JSON data"):
        postgres_module._dump_json(float("nan"), field_name="block value")


def test_postgres_reports_corrupt_stored_rows() -> None:
    """Unreadable stored data raises a corruption error, not a decode error."""

    with pytest.raises(BackendCorruption, match="JSON is invalid"):
        postgres_module._load_json("{not json")
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        postgres_module._decode_datetime("not a timestamp")
    with pytest.raises(BackendCorruption, match="timestamps must be timezone-aware"):
        postgres_module._decode_datetime("2024-01-01T00:00:00")


def test_postgres_advisory_lock_key_is_stable_and_schema_scoped() -> None:
    """The lock key is reproducible across processes and fits a signed int32."""

    key = postgres_module._advisory_lock_key("genie")

    assert key == postgres_module._advisory_lock_key("genie")
    assert key != postgres_module._advisory_lock_key("genie_other")
    for name in ("genie", "a", "g" * 63, "team_memory_2"):
        assert -(2**31) <= postgres_module._advisory_lock_key(name) < 2**31


class _FakeError(Exception):
    """Stand-in for a psycopg error carrying a SQLSTATE code."""

    def __init__(self, sqlstate: str | None, message: str = "failed") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeOperationalError(_FakeError):
    """Stand-in for a dropped connection, which carries no SQLSTATE."""

    __name__ = "OperationalError"


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("40001", BackendBusy),
        ("40P01", BackendBusy),
        ("55P03", BackendBusy),
        ("57014", BackendBusy),
        ("25006", BackendReadOnly),
        ("42501", BackendReadOnly),
        ("28000", BackendReadOnly),
        ("53100", BackendCapacityExceeded),
        ("53200", BackendCapacityExceeded),
        ("54001", BackendCapacityExceeded),
        ("XX001", BackendCorruption),
        ("XX002", BackendCorruption),
        ("23505", MemoryConflict),
        ("23503", MemoryConflict),
        ("08006", BackendUnavailable),
        ("53300", BackendUnavailable),
        ("57P03", BackendUnavailable),
        ("3D000", BackendUnavailable),
        ("42601", BackendError),
    ],
)
def test_postgres_translates_sqlstate_codes_to_stable_errors(
    sqlstate: str,
    expected: type[Exception],
) -> None:
    """Every mapped SQLSTATE class becomes the documented Genie error."""

    translated = postgres_module._translate_postgres_error(_FakeError(sqlstate))

    assert type(translated) is expected


def test_postgres_treats_a_dropped_connection_as_unavailable() -> None:
    """A driver failure with no SQLSTATE still reports storage as unavailable."""

    error = _FakeOperationalError(None, "server closed the connection")
    error.__class__.__name__ = "OperationalError"

    assert isinstance(postgres_module._translate_postgres_error(error), BackendUnavailable)


class _ProbeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _ProbeConnection:
    """Connection that answers only the server-role probe."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> _ProbeCursor:
        if isinstance(self._answer, BaseException):
            raise self._answer
        return _ProbeCursor(self._answer)

    def close(self) -> None:
        self.closed = True


def _probe_backend(answer: Any) -> PostgresBackend:
    backend = object.__new__(PostgresBackend)
    backend.schema = "genie"
    backend._connection = _ProbeConnection(answer)
    backend._exception_types = (_FakeError,)
    return backend


@pytest.mark.parametrize(
    "answer",
    [
        {"in_recovery": False},
        {"in_recovery": None},
        {},
        None,
        {"in_recovery": "no"},
        _FakeError("42883", "function pg_is_in_recovery() does not exist"),
    ],
    ids=["primary", "null", "missing-field", "no-row", "unreadable", "command-refused"],
)
def test_postgres_server_role_probe_passes_unless_recovery_is_proven(answer: Any) -> None:
    """Anything short of a proven replica lets construction continue."""

    _probe_backend(answer)._verify_server_role()


def test_postgres_server_role_probe_refuses_a_replica() -> None:
    """A server that reports recovery is refused with an actionable message."""

    with pytest.raises(UnsupportedOperation, match="read-only replica"):
        _probe_backend({"in_recovery": True})._verify_server_role()


def test_postgres_server_role_probe_lets_a_connection_failure_through() -> None:
    """An unreachable server is reported instead of being treated as unknown."""

    with pytest.raises(_FakeError):
        _probe_backend(_FakeError("08006", "connection failed"))._verify_server_role()


def test_postgres_backend_discards_connection_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected schema failure closes the new connection before escaping."""

    closed = Event()

    class FakeConnection:
        def execute(self, sql: str, params: Any = None) -> _ProbeCursor:
            return _ProbeCursor({"in_recovery": False})

        @contextmanager
        def transaction(self) -> Any:
            yield self

        def close(self) -> None:
            closed.set()

    class FakeDriver:
        Error = _FakeError
        IntegrityError = _FakeError

        @staticmethod
        def connect(*args: object, **kwargs: object) -> FakeConnection:
            return FakeConnection()

    monkeypatch.setattr(
        postgres_module,
        "_load_psycopg",
        lambda: (FakeDriver, dict, (_FakeError,), (_FakeError,)),
    )
    monkeypatch.setattr(
        PostgresBackend,
        "_initialize_schema",
        lambda self: (_ for _ in ()).throw(RuntimeError("unexpected schema failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected schema failure"):
        PostgresBackend("postgresql:///app")

    assert closed.is_set()


def test_postgres_schema_expectations_cover_every_owned_object() -> None:
    """The migration and the validation lists describe the same schema."""

    statements = postgres_module._MIGRATIONS[postgres_module._SCHEMA_VERSION]
    rendered = [statement.format(schema='"genie"') for statement in statements]

    for table in postgres_module._EXPECTED_TABLE_COLUMNS:
        assert any(f'CREATE TABLE IF NOT EXISTS "genie".{table} ' in statement for statement in rendered), table
    for index in postgres_module._EXPECTED_INDEXES:
        created = any(f"CREATE INDEX IF NOT EXISTS {index} " in statement for statement in rendered)
        constrained = any(f"CONSTRAINT {index} " in statement for statement in rendered)
        assert created or constrained, index
    for table, foreign_keys in postgres_module._EXPECTED_FOREIGN_KEYS.items():
        statement = next(item for item in rendered if f'CREATE TABLE IF NOT EXISTS "genie".{table} ' in item)
        assert statement.count("FOREIGN KEY") == len(foreign_keys), table
        assert statement.count("ON DELETE CASCADE") == len(foreign_keys), table


def test_postgres_initialization_checks_the_namespace_inside_the_locked_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record read, the table check, and the migration share one locked transaction.

    Reading outside the advisory lock could miss the record and then see the
    tables that another constructor committed in between. Inside the lock, a
    constructor that waits reads the record the first one committed.
    """

    statements: list[str] = []

    class RecordingCursor:
        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    class RecordingConnection:
        @contextmanager
        def transaction(self) -> Any:
            statements.append("BEGIN")
            yield
            statements.append("COMMIT")

        def execute(self, sql: str, params: Any = None) -> RecordingCursor:
            statements.append(" ".join(sql.split()))
            return RecordingCursor()

    backend = object.__new__(PostgresBackend)
    backend.schema = "genie"
    backend._schema_info = '"genie".schema_info'
    backend._connection = RecordingConnection()
    backend._exception_types = (_FakeError,)
    backend._lock = RLock()
    backend._advisory_lock_key = 1
    monkeypatch.setattr(PostgresBackend, "_validate_schema", lambda self: None)

    backend._initialize_schema()

    assert statements[0] == "BEGIN"
    assert statements[1].startswith("SELECT pg_advisory_xact_lock")
    assert statements[-1] == "COMMIT"
    assert statements.count("BEGIN") == 1
    body = statements[2:-1]
    assert body[0] == 'CREATE SCHEMA IF NOT EXISTS "genie"'
    assert sum("information_schema.tables" in statement for statement in body) == 2
    assert any(statement.startswith("INSERT INTO") and "schema_info" in statement for statement in body)


_LIVE_DSN = os.environ.get("GENIE_POSTGRES_DSN")
_LIVE_DRIVER_AVAILABLE = importlib.util.find_spec("psycopg") is not None
_LIVE_REASON = "set GENIE_POSTGRES_DSN and install the postgres extra to run live PostgreSQL tests"
_LIVE_SCHEMA = os.environ.get("GENIE_POSTGRES_SCHEMA", "genie_test")


def _live_backend(*, initialize_schema: bool = True) -> PostgresBackend:
    assert _LIVE_DSN is not None
    return PostgresBackend(
        _LIVE_DSN,
        schema=_LIVE_SCHEMA,
        initialize_schema=initialize_schema,
    )


def _clear_live_data(backend: PostgresBackend) -> None:
    # Deleting every root block cascades to the whole dataset. The acyclic
    # protections are not block-owned, so they are cleared on their own.
    with backend._write_transaction() as connection:
        connection.execute(f"DELETE FROM {backend._blocks}")
        connection.execute(f"DELETE FROM {backend._acyclic_relations}")


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestPostgresStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
):
    """Run every reusable capability contract against a live PostgreSQL server."""

    def setup_method(self) -> None:
        self.backend = _live_backend()
        _clear_live_data(self.backend)

    def teardown_method(self) -> None:
        _clear_live_data(self.backend)
        self.backend.close()

    def make_backend(self) -> PostgresBackend:
        return self.backend


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_postgres_backend_persists_and_coordinates_instances() -> None:
    """Committed state survives reconnects and concurrent instances share the lock."""

    first = _live_backend()
    _clear_live_data(first)
    log = first.put_block(MemoryBlock(id="log", type="log", value={}))
    target = first.put_block(MemoryBlock(id="target", type="event", value={}))
    first.close()

    left = _live_backend(initialize_schema=False)
    right = _live_backend(initialize_schema=False)
    try:
        assert left.get_block(log.id) == log
        with ThreadPoolExecutor(max_workers=8) as executor:
            entries = list(
                executor.map(
                    lambda index: (left if index % 2 == 0 else right).append_log_entry(log.id, target.id),
                    range(40),
                )
            )
        assert sorted(entry.sequence for entry in entries) == list(range(40))

        source = left.put_block(MemoryBlock(id="source", type="node", value={}))
        destination = left.put_block(MemoryBlock(id="destination", type="node", value={}))

        def add(backend: PostgresBackend, link: MemoryLink) -> bool:
            try:
                backend.add_acyclic_link(link)
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda item: add(*item),
                    [
                        (left, MemoryLink(source.id, destination.id, "lineage")),
                        (right, MemoryLink(destination.id, source.id, "lineage")),
                    ],
                )
            )
        assert sorted(outcomes) == [False, True]
    finally:
        _clear_live_data(left)
        left.close()
        right.close()


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_postgres_orders_text_by_bytes_not_by_locale() -> None:
    """Paging uses byte order, so it matches the other relational backend."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        collection = backend.put_block(MemoryBlock(id="collection", type="timeline", value={}))
        # Under a locale collation such as en_US.UTF-8 these ids sort in a
        # different order than plain byte order puts them in.
        ids = ["a-1", "A-2", "a_3", "AB", "ab"]
        moment = datetime(2024, 1, 1, tzinfo=UTC)
        for block_id in ids:
            backend.put_block(MemoryBlock(id=block_id, type="event", value={}))
            backend.add_temporal_entry(
                TemporalEntry(
                    collection_id=collection.id,
                    block_id=block_id,
                    occurred_at=moment,
                )
            )

        listed = [entry.block_id for entry in backend.list_temporal_entries(collection.id, limit=10)]
        assert listed == sorted(ids)
    finally:
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_postgres_backend_serves_the_public_memory_api() -> None:
    """The adapter works behind `Memory` with scoped search and ownership."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        memory = Memory(backend=backend, principal="agent:planner")
        root = memory.put(type="project", value={"name": "genie"})
        memory.put(type="note", value={"text": "persist this"}, parent=root.id)

        results = memory.search(MemoryQuery(text="persist", root_id=root.id, scope=Scope.SUBTREE))

        assert [result.block.type for result in results] == ["note"]
    finally:
        _clear_live_data(backend)
        backend.close()


_LIVE_INIT_SCHEMA = f"{_LIVE_SCHEMA}_init"


def _live_init_backend() -> PostgresBackend:
    """Open a backend on the schema reserved for initialization tests."""

    assert _LIVE_DSN is not None
    return PostgresBackend(_LIVE_DSN, schema=_LIVE_INIT_SCHEMA)


def _drop_live_init_schema() -> None:
    """Remove the initialization test schema so the next test starts empty."""

    with _live_backend() as helper, helper._write_transaction() as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{_LIVE_INIT_SCHEMA}" CASCADE')


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_postgres_backend_coordinates_concurrent_initializers() -> None:
    """Concurrent constructors on one empty schema all succeed."""

    _drop_live_init_schema()

    with ThreadPoolExecutor(max_workers=8) as executor:
        backends = list(executor.map(lambda _: _live_init_backend(), range(8)))
    try:
        for backend in backends:
            backend._validate_schema()
    finally:
        for backend in backends:
            backend.close()
        _drop_live_init_schema()


@pytest.mark.skipif(not (_LIVE_DSN and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_postgres_backend_refuses_a_schema_with_foreign_tables() -> None:
    """Tables that no Genie record owns are refused and left alone."""

    _drop_live_init_schema()
    with _live_backend() as helper:
        with helper._write_transaction() as connection:
            connection.execute(f'CREATE SCHEMA "{_LIVE_INIT_SCHEMA}"')
            connection.execute(f'CREATE TABLE "{_LIVE_INIT_SCHEMA}".notes (id integer)')
        try:
            with pytest.raises(UnsupportedOperation, match="no Genie schema record"):
                _live_init_backend()
            with helper._read_transaction() as connection:
                rows = connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (_LIVE_INIT_SCHEMA,),
                ).fetchall()
            assert [row["table_name"] for row in rows] == ["notes"]
        finally:
            _drop_live_init_schema()
