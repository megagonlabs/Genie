"""Unit tests and optional live tests for the Neo4j backend that uses the official driver."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Condition, Event, RLock
from typing import Any, Literal, Self, cast

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
    Neo4jBackend,
    RouteCatalog,
    RouteInfo,
    Scope,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.graph import neo4j as neo4j_module
from genie.structures import KeyBinding, MemoryAddress, TemporalEntry
from tests.backends.test_backend_contracts import (
    AcyclicLinkStoreContract,
    BlockStoreContract,
    KeyedStoreContract,
    LinkStoreContract,
    RouteCatalogContract,
    SearchIndexContract,
    SequenceStoreContract,
    TemporalIndexContract,
    TemporalStoreContract,
    TraversalIndexContract,
)


def test_neo4j_backend_implements_every_backend_protocol() -> None:
    """`Neo4jBackend` implements `MemoryBackend` and every optional capability protocol."""

    backend = object.__new__(Neo4jBackend)

    assert isinstance(backend, MemoryBackend)
    assert isinstance(backend, AcyclicLinkStore)
    assert isinstance(backend, KeyedStore)
    assert isinstance(backend, SequenceStore)
    assert isinstance(backend, TemporalStore)
    assert isinstance(backend, TemporalIndex)
    assert isinstance(backend, RouteCatalog)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"uri": 1}, TypeError, "uri must be a string"),
        ({"uri": ""}, ValueError, "uri must not be empty"),
        ({"uri": "neo4j://example", "database": ""}, ValueError, "database must not be empty"),
        ({"uri": "neo4j://example", "connection_timeout": True}, TypeError, "connection_timeout must be a number"),
        ({"uri": "neo4j://example", "connection_timeout": -1}, ValueError, "connection_timeout must be a finite number"),
        ({"uri": "neo4j://example", "max_transaction_retry_time": float("inf")}, ValueError, "max_transaction_retry_time must be a finite number"),
        ({"uri": "neo4j://example", "auth": ["neo4j", "password"]}, TypeError, "auth must be"),
        ({"uri": "neo4j://example", "encrypted": "yes"}, TypeError, "encrypted must be"),
        ({"uri": "neo4j://example", "verify_connectivity": 1}, TypeError, "verify_connectivity must be"),
        ({"uri": "neo4j://example", "initialize_schema": 1}, TypeError, "initialize_schema must be"),
    ],
)
def test_neo4j_backend_rejects_invalid_configuration_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """`Neo4jBackend` rejects an invalid configuration before it loads the driver, so it does not open a network connection."""

    monkeypatch.setattr(
        neo4j_module,
        "_load_neo4j_driver",
        lambda: pytest.fail("driver should not be loaded"),
    )

    with pytest.raises(error, match=message):
        Neo4jBackend(**kwargs)


def test_neo4j_backend_explains_its_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the `neo4j` package is not installed, `Neo4jBackend` raises `UnsupportedOperation` with the command that installs the `neo4j` extra."""

    # A `None` entry in `sys.modules` makes the import fail, also when the driver is installed.
    monkeypatch.setitem(sys.modules, "neo4j", None)

    with pytest.raises(UnsupportedOperation) as caught:
        Neo4jBackend("neo4j://example")

    assert str(caught.value) == "Neo4jBackend requires the optional 'neo4j' package. In the Genie clone, run: python -m pip install -e '.[neo4j]'"


def test_neo4j_serialization_is_strict_and_defensive() -> None:
    """The graph property form keeps the JSON value types of a block."""

    original = MemoryBlock(
        id="note",
        type="note",
        value={"items": [1, True, None]},
        metadata={"nested": {"value": "kept"}},
    )
    properties = neo4j_module._block_properties(original)
    reconstructed = neo4j_module._block_from_record({"block": {**properties, "storage_order": 0}, "parent_id": None})

    assert reconstructed == original
    reconstructed.value["items"].append("local")
    assert neo4j_module._block_from_record({"block": {**properties, "storage_order": 0}, "parent_id": None}).value == {"items": [1, True, None]}
    with pytest.raises(TypeError, match="strict JSON"):
        neo4j_module._block_properties(MemoryBlock(type="invalid", value=(1, 2)))


def test_neo4j_serialization_reports_corrupt_stored_values() -> None:
    """Malformed database properties raise `BackendCorruption`, not an unrelated parser error."""

    properties = neo4j_module._block_properties(MemoryBlock(id="note", type="note", value={}))
    properties["value_json"] = "{not-json"

    with pytest.raises(BackendCorruption, match="JSON"):
        neo4j_module._block_from_record({"block": {**properties, "storage_order": 0}, "parent_id": None})


def test_neo4j_serialization_rejects_non_strict_json_and_policy_booleans() -> None:
    """A stored non-finite value or a policy boolean stored as a truthy string raises `BackendCorruption`."""

    properties = neo4j_module._block_properties(
        MemoryBlock(
            id="note",
            type="note",
            value={},
            policy=MemoryPolicy.public(),
        )
    )
    properties["value_json"] = "NaN"
    with pytest.raises(BackendCorruption, match="strict JSON"):
        neo4j_module._block_from_record({"block": {**properties, "storage_order": 0}, "parent_id": None})


def test_neo4j_serialization_rejects_invalid_versions_times_and_namespaces() -> None:
    """Malformed caller values raise `TypeError` or `ValueError`, and malformed stored primitives raise `BackendCorruption`."""

    with pytest.raises(ValueError, match="block version"):
        neo4j_module._block_properties(MemoryBlock(id="note", type="note", value={}, version=0))
    with pytest.raises(TypeError, match="created_at must be a datetime"):
        neo4j_module._encode_datetime(cast(datetime, "bad"), field_name="created_at")
    with pytest.raises(ValueError, match="created_at must be timezone-aware"):
        neo4j_module._encode_datetime(
            datetime(2026, 1, 1),  # noqa: DTZ001 - deliberately malformed input
            field_name="created_at",
        )
    with pytest.raises(BackendCorruption, match="JSON values must be strings"):
        neo4j_module._load_json(1)
    with pytest.raises(BackendCorruption, match="timestamps must be strings"):
        neo4j_module._decode_datetime(1)
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        neo4j_module._decode_datetime("not-a-timestamp")
    with pytest.raises(BackendCorruption, match="timezone-aware"):
        neo4j_module._decode_datetime("2026-01-01T00:00:00")
    with pytest.raises(BackendCorruption, match="string lists"):
        neo4j_module._decode_namespace("{}")


@pytest.mark.parametrize(
    ("decoder", "record", "message"),
    [
        (neo4j_module._link_from_record, {"link": {}}, "memory link"),
        (neo4j_module._binding_from_record, {"binding": {}}, "key binding"),
        (neo4j_module._log_entry_from_record, {"entry": {}}, "log entry"),
        (neo4j_module._temporal_from_record, {"entry": {}}, "temporal entry"),
        (neo4j_module._route_entry_from_record, {"entry": {}}, "route catalog entry"),
        (neo4j_module._route_entry_from_record, {"entry": {"block_id": "block", "route": 1}}, "route catalog entry"),
    ],
)
def test_neo4j_record_decoders_translate_malformed_shapes(
    decoder: Any,
    record: dict[str, dict[str, object]],
    message: str,
) -> None:
    """Each decoder for an optional stored record translates a malformed shape to `BackendCorruption`."""

    with pytest.raises(BackendCorruption, match=message):
        decoder(record)

    properties = neo4j_module._block_properties(
        MemoryBlock(
            id="note",
            type="note",
            value={},
            policy=MemoryPolicy.public(),
        )
    )
    policy = neo4j_module._load_json(properties["policy_json"])
    policy["read"]["allow_all"] = "false"
    properties["policy_json"] = json.dumps(policy)
    with pytest.raises(BackendCorruption, match="memory block"):
        neo4j_module._block_from_record({"block": {**properties, "storage_order": 0}, "parent_id": None})


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("Neo.TransientError.Transaction.DeadlockDetected", "deadlock", BackendBusy),
        ("Neo.ClientError.General.ForbiddenOnReadOnlyDatabase", "read only", BackendReadOnly),
        ("Neo.TransientError.General.MemoryPoolOutOfMemoryError", "memory limit", BackendCapacityExceeded),
        ("Neo.DatabaseError.General.UnknownError", "corrupt store", BackendCorruption),
        ("Neo.TransientError.Network.CommunicationError", "connection lost", BackendUnavailable),
        ("Neo.ClientError.Schema.ConstraintValidationFailed", "constraint", MemoryConflict),
        ("Neo.ClientError.Security.Unauthorized", "authentication failed", BackendError),
    ],
)
def test_neo4j_driver_errors_map_to_public_errors(
    code: str,
    message: str,
    expected: type[Exception],
) -> None:
    """The backend translates driver errors to public errors, so driver-specific details do not reach the caller."""

    class DriverFailure(Exception):
        def __init__(self) -> None:
            super().__init__(message)
            self.code = code

        def is_retryable(self) -> bool:
            return False

    assert isinstance(neo4j_module._translate_neo4j_error(DriverFailure()), expected)


def test_neo4j_driver_error_mapping_handles_retryable_and_unknown_failures() -> None:
    """A retryable driver error becomes `BackendUnavailable`, and an unknown driver error becomes `BackendError`."""

    class RetryableFailure(Exception):
        def is_retryable(self) -> bool:
            return True

    class UnknownFailure(Exception):
        def is_retryable(self) -> bool:
            return False

    assert isinstance(
        neo4j_module._translate_neo4j_error(RetryableFailure("temporary")),
        BackendUnavailable,
    )
    assert isinstance(
        neo4j_module._translate_neo4j_error(UnknownFailure()),
        BackendError,
    )


def test_neo4j_backend_close_is_idempotent_and_enforces_lifecycle() -> None:
    """`close` releases the owned driver only one time, and later operations raise `RuntimeError`."""

    class FakeDriver:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    driver = FakeDriver()
    backend = object.__new__(Neo4jBackend)
    backend._driver = driver
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._active_operations = 0
    backend._closing = False
    backend._exception_types = ()

    backend.close()
    backend.close()

    assert driver.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        backend.get_block("missing")


def test_neo4j_backend_close_waits_for_active_operations() -> None:
    """`close` waits until a session that already leased the driver finishes."""

    operation_started = Event()
    release_operation = Event()
    driver_closed = Event()

    class FakeSession:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute_read(self, operation: object) -> None:
            operation_started.set()
            assert release_operation.wait(timeout=2)

    class FakeDriver:
        def session(self, **kwargs: object) -> FakeSession:
            return FakeSession()

        def close(self) -> None:
            driver_closed.set()

    backend = object.__new__(Neo4jBackend)
    backend._driver = FakeDriver()
    backend._bookmark_manager = object()
    backend.database = "neo4j"
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._active_operations = 0
    backend._closing = False
    backend._exception_types = ()

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation = executor.submit(backend.get_block, "missing")
        assert operation_started.wait(timeout=2)
        closing = executor.submit(backend.close)
        with backend._lifecycle:
            assert backend._lifecycle.wait_for(
                lambda: backend._closing,
                timeout=2,
            )
        assert not driver_closed.is_set()
        release_operation.set()
        assert operation.result(timeout=2) is None
        closing.result(timeout=2)

    assert driver_closed.is_set()


def test_neo4j_backend_translates_driver_close_failures() -> None:
    """If the driver fails to close, the backend clears its lifecycle state and raises a public error."""

    class DriverFailure(Exception):
        code = "Neo.TransientError.Network.CommunicationError"

    class FakeDriver:
        def close(self) -> None:
            raise DriverFailure("connection lost")

    backend = object.__new__(Neo4jBackend)
    backend._driver = FakeDriver()
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._active_operations = 0
    backend._closing = False
    backend._exception_types = (DriverFailure,)

    with pytest.raises(BackendUnavailable, match="unavailable"):
        backend.close()

    assert backend._driver is None
    assert backend._closing is False


def test_neo4j_backend_forwards_production_driver_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection, retry, encryption, and auth settings reach the driver."""

    calls: dict[str, Any] = {}

    class FakeDriver:
        def verify_connectivity(self) -> None:
            calls["verified"] = True

        def close(self) -> None:
            calls["closed"] = True

    class FakeGraphDatabase:
        @staticmethod
        def bookmark_manager() -> object:
            return "bookmark-manager"

        @staticmethod
        def driver(uri: str, *, auth: object, **options: object) -> FakeDriver:
            calls.update(uri=uri, auth=auth, options=options)
            return FakeDriver()

    monkeypatch.setattr(
        neo4j_module,
        "_load_neo4j_driver",
        lambda: (FakeGraphDatabase, ()),
    )
    monkeypatch.setattr(Neo4jBackend, "_initialize_schema", lambda self: None)

    backend = Neo4jBackend(
        "neo4j+s://example.test:7687",
        auth=("neo4j", "secret"),
        connection_timeout=4.5,
        max_transaction_retry_time=12.0,
        encrypted=True,
    )
    backend.close()

    assert calls == {
        "uri": "neo4j+s://example.test:7687",
        "auth": ("neo4j", "secret"),
        "options": {
            "connection_timeout": 4.5,
            "max_transaction_retry_time": 12.0,
            "encrypted": True,
        },
        "verified": True,
        "closed": True,
    }


def test_neo4j_backend_discards_driver_after_unexpected_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `_initialize_schema` fails with an unexpected error, the backend closes the new driver before the error reaches the caller."""

    closed = Event()

    class FakeDriver:
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            closed.set()

    class FakeGraphDatabase:
        @staticmethod
        def bookmark_manager() -> object:
            return object()

        @staticmethod
        def driver(*args: object, **kwargs: object) -> FakeDriver:
            return FakeDriver()

    monkeypatch.setattr(
        neo4j_module,
        "_load_neo4j_driver",
        lambda: (FakeGraphDatabase, ()),
    )
    monkeypatch.setattr(
        Neo4jBackend,
        "_initialize_schema",
        lambda self: (_ for _ in ()).throw(RuntimeError("unexpected schema failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected schema failure"):
        Neo4jBackend("neo4j://example")

    assert closed.is_set()


def test_neo4j_schema_statement_treats_an_existing_rule_as_success() -> None:
    """The backend treats a `CREATE ... IF NOT EXISTS` statement that loses a race to another constructor as a success.

    - **The lost race.** Neo4j answers the constructor that loses the race with
      an "already exists" schema error.
    - **Other failures.** Any other failure of a schema statement still goes to
      the caller.
    """

    class SchemaRuleExists(Exception):
        code = "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"

    class SyntaxFailure(Exception):
        code = "Neo.ClientError.Statement.SyntaxError"

    answers: list[Exception] = [SchemaRuleExists("An equivalent index already exists"), SyntaxFailure("Invalid input")]

    class FakeSession:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute_write(self, operation: object) -> None:
            raise answers.pop(0)

    class FakeDriver:
        def session(self, **kwargs: object) -> FakeSession:
            return FakeSession()

    backend = object.__new__(Neo4jBackend)
    backend._driver = FakeDriver()
    backend._bookmark_manager = object()
    backend.database = "neo4j"
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._active_operations = 0
    backend._closing = False
    backend._exception_types = (SchemaRuleExists, SyntaxFailure)

    backend._schema_statement("CREATE INDEX genie_block_type_idx IF NOT EXISTS FOR (n:GenieBlock) ON (n.type)")
    with pytest.raises(BackendError, match="Invalid input"):
        backend._schema_statement("CREATE INDEX")
    assert answers == []


_LIVE_URI = os.environ.get("GENIE_NEO4J_URI")
_LIVE_DRIVER_AVAILABLE = importlib.util.find_spec("neo4j") is not None
_LIVE_REASON = "set GENIE_NEO4J_URI and install the neo4j extra to run live Neo4j tests"


def _live_auth() -> tuple[str, str] | None:
    user = os.environ.get("GENIE_NEO4J_USER")
    password = os.environ.get("GENIE_NEO4J_PASSWORD")
    if user is None and password is None:
        return None
    if user is None or password is None:
        raise RuntimeError("GENIE_NEO4J_USER and GENIE_NEO4J_PASSWORD must be set together.")
    return user, password


def _live_backend(*, initialize_schema: bool = True) -> Neo4jBackend:
    assert _LIVE_URI is not None
    return Neo4jBackend(
        _LIVE_URI,
        auth=_live_auth(),
        database=os.environ.get("GENIE_NEO4J_DATABASE", "neo4j"),
        initialize_schema=initialize_schema,
    )


def _clear_live_data(backend: Neo4jBackend) -> None:
    backend._raw_write(
        lambda tx: neo4j_module._consume(
            tx,
            "MATCH (n) WHERE any(label IN labels(n) WHERE label STARTS WITH 'Genie') AND NOT n:GenieSchema DETACH DELETE n",
        )
    )


def _clear_live_schema(backend: Neo4jBackend) -> None:
    """Remove every Genie node, constraint, and index, so that the graph is empty."""

    backend._raw_write(
        lambda tx: neo4j_module._consume(
            tx,
            "MATCH (n) WHERE any(label IN labels(n) WHERE label STARTS WITH 'Genie') DETACH DELETE n",
        )
    )
    for name in neo4j_module._CONSTRAINTS:
        backend._schema_statement(f"DROP CONSTRAINT {name} IF EXISTS")
    for name in neo4j_module._INDEXES:
        backend._schema_statement(f"DROP INDEX {name} IF EXISTS")


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestNeo4jStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    SearchIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
    RouteCatalogContract,
):
    """Run every reusable capability contract against a live Neo4j database."""

    def setup_method(self) -> None:
        self.backend = _live_backend()
        _clear_live_data(self.backend)

    def teardown_method(self) -> None:
        _clear_live_data(self.backend)
        self.backend.close()

    def make_backend(self) -> Neo4jBackend:
        return self.backend


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_persists_and_coordinates_instances() -> None:
    """Committed state stays after a reconnect, and concurrent instances share locks."""

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
        left.protect_relation("lineage")

        def add(backend: Neo4jBackend, link: MemoryLink) -> bool:
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
        assert outcomes.count(True) == 1
        # The protection is stored in the database, so the other instance
        # refuses a plain link with the relation.
        with pytest.raises(MemoryConflict, match="protected as an acyclic graph"):
            right.add_link(MemoryLink(source.id, destination.id, "lineage"))
    finally:
        _clear_live_data(left)
        left.close()
        right.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_route_catalog_claims_one_route_across_instances() -> None:
    """Of 2 instances that claim one block id at the same time, each with its own route, only one succeeds."""

    left = _live_backend()
    _clear_live_data(left)
    right = _live_backend(initialize_schema=False)
    barrier = Barrier(2)

    def claim(backend: Neo4jBackend, entry: RouteInfo) -> bool:
        barrier.wait()
        try:
            backend.claim_route(entry)
        except MemoryConflict:
            return False
        return True

    try:
        entries = [RouteInfo("shared", "us"), RouteInfo("shared", "eu")]
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, [left, right], entries))

        assert outcomes.count(True) == 1
        winner = entries[outcomes.index(True)]
        assert left.get_route("shared") == winner
        assert right.get_route("shared") == winner
        right.release_routes(["shared"])
        assert left.get_route("shared") is None
    finally:
        _clear_live_data(left)
        left.close()
        right.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_preserves_link_identity_and_rolls_back_conflicts() -> None:
    """A failed topology change is atomic, and a change to returned link metadata does not change the stored link."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        root = backend.put_block(MemoryBlock(id="root", type="root", value={}))
        child = backend.put_block(MemoryBlock(id="child", parent_id=root.id, type="child", value={}))
        with pytest.raises(MemoryConflict):
            backend.update_block(replace(root, parent_id=child.id))
        assert backend.children(root.id) == [child]

        link = backend.add_link(MemoryLink(root.id, child.id, "contains", id="link", metadata={"tags": ["initial"]}))
        listed = backend.list_links(root.id)
        listed[0].metadata["tags"].append("local")
        assert backend.list_links(root.id) == [link]
        assert backend.update_link(replace(link, metadata={"tags": ["updated"]})).metadata == {"tags": ["updated"]}
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_rejects_invalid_link_mutations_and_directions() -> None:
    """The Neo4j backend enforces link identity, link existence, and valid read directions."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        source = backend.put_block(MemoryBlock(id="source", type="node", value={}))
        target = backend.put_block(MemoryBlock(id="target", type="node", value={}))
        link = backend.add_link(MemoryLink(source.id, target.id, "related", id="link"))

        with pytest.raises(MemoryConflict, match="does not exist"):
            backend.update_link(MemoryLink(source.id, target.id, "related", id="missing"))
        with pytest.raises(MemoryConflict, match="only metadata"):
            backend.update_link(replace(link, target_id=source.id))
        with pytest.raises(ValueError, match="direction"):
            backend.list_links(source.id, direction=cast(Literal["out", "in", "both"], "sideways"))

        assert backend.list_links(target.id, direction="in") == [link]
        assert backend.list_links(source.id, relation="unrelated") == []
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_searches_every_scope_and_filters_candidates() -> None:
    """Portable search on real records filters candidates by scope, type, and text."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        root = backend.put_block(MemoryBlock(id="root", type="root", value={"text": "alpha"}))
        child = backend.put_block(
            MemoryBlock(
                id="child",
                parent_id=root.id,
                type="child",
                value={"text": "beta"},
            )
        )
        leaf = backend.put_block(
            MemoryBlock(
                id="leaf",
                parent_id=child.id,
                type="leaf",
                value={"text": "gamma"},
            )
        )

        assert [result.block.id for result in backend.search(MemoryQuery(root_id=child.id, scope=Scope.SELF))] == [child.id]
        assert sorted(result.block.id for result in backend.search(MemoryQuery(root_id=leaf.id, scope=Scope.ANCESTORS))) == sorted([leaf.id, child.id, root.id])
        assert backend.search(MemoryQuery(root_id="missing", scope=Scope.SELF)) == []
        assert backend.search(MemoryQuery(types=["missing-type"])) == []
        assert backend.search(MemoryQuery("missing text")) == []
        assert {result.block.id for result in backend.search(MemoryQuery())} == {
            root.id,
            child.id,
            leaf.id,
        }
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_reports_corrupt_persisted_record_types() -> None:
    """Malformed Neo4j properties raise `BackendCorruption` for blocks, links, keys, log entries, temporal entries, and route catalog entries."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        container = backend.put_block(MemoryBlock(id="container", type="container", value={}))
        target = backend.put_block(MemoryBlock(id="target", type="item", value={}))
        corrupt = backend.put_block(MemoryBlock(id="corrupt", type="item", value={}))
        link = backend.add_link(MemoryLink(container.id, target.id, "related", id="link"))
        address = MemoryAddress(("items",), "target")
        backend.bind_key(KeyBinding(container.id, address, target.id))
        log_entry = backend.append_log_entry(container.id, target.id)
        temporal_entry = backend.add_temporal_entry(
            TemporalEntry(
                container.id,
                target.id,
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        backend.claim_route(RouteInfo(target.id, "us"))
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (b:GenieBlock {id: $id}) SET b.created_at = 1",
                id=corrupt.id,
            )
        )
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (n:GenieRouteEntry {block_id: $id}) SET n.route = 1",
                id=target.id,
            )
        )
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (l:GenieLink {id: $id}) SET l.created_at = 1",
                id=link.id,
            )
        )
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (n:GenieKeyBinding {collection_id: $id}) SET n.namespace_json = '{}'",
                id=container.id,
            )
        )
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (n:GenieLogEntry {identity: $identity}) SET n.sequence = 'invalid'",
                identity=neo4j_module._identity(
                    log_entry.log_id,
                    log_entry.sequence,
                ),
            )
        )
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (n:GenieTemporalEntry {identity: $identity}) SET n.occurred_at = 'invalid'",
                identity=neo4j_module._identity(
                    temporal_entry.collection_id,
                    temporal_entry.block_id,
                ),
            )
        )

        with pytest.raises(BackendCorruption, match="timestamps"):
            backend.get_block(corrupt.id)
        with pytest.raises(BackendCorruption, match="timestamps"):
            backend.list_links(container.id)
        with pytest.raises(BackendCorruption, match="namespace"):
            backend.list_keys(container.id)
        with pytest.raises(BackendCorruption, match="log entry"):
            backend.list_log_entries(container.id)
        with pytest.raises(BackendCorruption, match="timestamp"):
            backend.get_temporal_entry(container.id, target.id)
        with pytest.raises(BackendCorruption, match="route catalog entry"):
            backend.get_route(target.id)
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_matches_core_ownership_search_and_delete_behavior() -> None:
    """Ownership, scoped search, policies, and recursive cleanup match the core behavior."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        memory = Memory(backend=backend, principal="owner")
        root = memory.put(id="root", type="project", value={"name": "search"})
        first = memory.put(
            id="first",
            parent=root,
            type="experiment",
            value={"result": "reranker improved precision"},
            metadata={"status": "complete"},
        )
        second = memory.put(id="second", parent=root, type="experiment", value={"result": "baseline"})
        link = memory.add_link(first, second, "compares_to", metadata={"run": 1})

        assert backend.children(root.id) == [first, second]
        assert backend.ancestors(first.id) == [root]
        assert backend.descendants(root.id) == [first, second]
        assert backend.contains(root.id, first.id)
        assert [
            result.block.id
            for result in backend.search(
                MemoryQuery(
                    "reranker",
                    root_id=root.id,
                    scope=Scope.SUBTREE,
                    filters={"metadata.status": "complete"},
                )
            )
        ] == [first.id]
        assert {result.block.id for result in backend.search(MemoryQuery(root_id=first.id, scope=Scope.LINKS))} == {
            first.id,
            second.id,
        }
        assert memory.list_links(first) == [link]

        memory.delete(root)
        assert backend.get_block(root.id) is None
        assert backend.get_block(first.id) is None
        assert backend.get_block(second.id) is None
        assert backend.list_links(first.id, direction="both") == []
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_explicit_cleanup_capabilities() -> None:
    """Every explicit cleanup operation has the same behavior as SQLite."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        container = backend.put_block(MemoryBlock(id="container", type="container", value={}))
        first = backend.put_block(MemoryBlock(id="first", type="item", value={}))
        second = backend.put_block(MemoryBlock(id="second", type="item", value={}))

        address = MemoryAddress(("tasks",), "first")
        backend.bind_key(KeyBinding(container.id, address, first.id))
        backend.delete_key_bindings_for_block(first.id)
        assert backend.get_key(container.id, address) is None

        assert backend.append_log_entry(container.id, first.id).sequence == 0
        backend.delete_log_entries_for_block(first.id)
        assert backend.list_log_entries(container.id) == []
        assert backend.append_log_entry(container.id, second.id).sequence == 1

        entry = TemporalEntry(
            container.id,
            first.id,
            occurred_at=first.created_at,
        )
        backend.add_temporal_entry(entry)
        backend.remove_temporal_entry(container.id, first.id)
        assert backend.get_temporal_entry(container.id, first.id) is None
        with pytest.raises(MemoryConflict, match="is not in temporal collection"):
            backend.remove_temporal_entry(container.id, first.id)
        backend.add_temporal_entry(entry)
        backend.delete_temporal_entries_for_block(first.id)
        assert backend.get_temporal_entry(container.id, first.id) is None

        backend.add_link(MemoryLink(first.id, second.id, "related"))
        backend.delete_links_for_block(first.id)
        assert backend.list_links(first.id, direction="both") == []
        assert backend.list_links(second.id, direction="both") == []
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_rejects_stale_writes_across_instances() -> None:
    """Locks in the database protect compare-and-swap (CAS) writes and subtree snapshots across drivers."""

    first = _live_backend()
    _clear_live_data(first)
    second = _live_backend(initialize_schema=False)
    try:
        root = first.put_block(MemoryBlock(id="root", type="root", value={}, policy=MemoryPolicy.public()))
        left_snapshot = first.get_block(root.id)
        right_snapshot = second.get_block(root.id)
        assert left_snapshot is not None
        assert right_snapshot is not None
        first.update_block(left_snapshot.with_updates(value={"left": True}, version=2), expected_version=1)
        with pytest.raises(MemoryConflict, match="changed"):
            second.update_block(right_snapshot.with_updates(value={"right": True}, version=2), expected_version=1)

        current = first.get_block(root.id)
        assert current is not None
        child = second.put_block(MemoryBlock(id="child", parent_id=root.id, type="child", value={}))
        with pytest.raises(MemoryConflict, match="changed"):
            first.delete_block(root.id, expected_blocks=[current])
        assert first.get_block(child.id) == child
    finally:
        _clear_live_data(first)
        first.close()
        second.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_rejects_future_and_incompatible_schema() -> None:
    """Runtime instances reject unsupported versions and wrong schema objects."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.application_id = 'other'",
            )
        )
        with pytest.raises(UnsupportedOperation, match="another application"):
            _live_backend()
        with pytest.raises(UnsupportedOperation, match="another application"):
            _live_backend(initialize_schema=False)
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.application_id = $application_id",
                application_id=neo4j_module._APPLICATION_ID,
            )
        )

        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = 999",
            )
        )
        with pytest.raises(UnsupportedOperation, match="newer"):
            _live_backend(initialize_schema=False)
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = $version",
                version=neo4j_module._SCHEMA_VERSION,
            )
        )

        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = 'invalid'",
            )
        )
        with pytest.raises(UnsupportedOperation, match="version is invalid"):
            _live_backend(initialize_schema=False)
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = 0",
            )
        )
        with pytest.raises(UnsupportedOperation, match="No Neo4j migration"):
            _live_backend(initialize_schema=False)
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = $version",
                version=neo4j_module._SCHEMA_VERSION,
            )
        )

        backend._schema_statement("DROP CONSTRAINT genie_block_id_unique IF EXISTS")
        with pytest.raises(UnsupportedOperation, match="constraint.*missing"):
            _live_backend(initialize_schema=False)
        backend._initialize_schema()

        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) DELETE m",
            )
        )
        with pytest.raises(UnsupportedOperation, match="not initialized"):
            _live_backend(initialize_schema=False)
        backend._initialize_schema()

        backend._schema_statement("DROP INDEX genie_block_type_idx IF EXISTS")
        with pytest.raises(UnsupportedOperation, match="missing or incompatible"):
            _live_backend(initialize_schema=False)

        backend._schema_statement("CREATE TEXT INDEX genie_block_type_idx FOR (n:GenieBlock) ON (n.type)")
        with pytest.raises(UnsupportedOperation, match="missing or incompatible"):
            _live_backend(initialize_schema=False)
    finally:
        backend._schema_statement("DROP INDEX genie_block_type_idx IF EXISTS")
        backend._initialize_schema()
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_coordinates_concurrent_initializers() -> None:
    """Concurrent constructors on an empty graph all succeed and leave one valid schema.

    - **Empty graph first.** The test empties the graph, so every constructor
      runs the schema statements at the same time.
    - **Expected errors.** Neo4j answers some statements with an "already
      exists" error, and the backend treats that error as a success.
    """

    with _live_backend() as helper:
        _clear_live_schema(helper)

    def initialize(_: int) -> Neo4jBackend:
        return _live_backend()

    with ThreadPoolExecutor(max_workers=8) as executor:
        backends = list(executor.map(initialize, range(8)))
    try:
        for backend in backends:
            backend._validate_schema()
    finally:
        for backend in backends:
            backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_resumes_version_zero_migration() -> None:
    """The backend migrates an interrupted or legacy schema at version 0 to the current version."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        backend._raw_write(
            lambda tx: neo4j_module._consume(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) SET m.version = 0",
            )
        )
        backend._schema_statement("DROP INDEX genie_block_type_idx IF EXISTS")

        with _live_backend() as migrated:
            state = migrated._read_schema_identity()
            assert state == {
                "application_id": neo4j_module._APPLICATION_ID,
                "version": neo4j_module._SCHEMA_VERSION,
            }
            migrated._validate_schema_objects()
    finally:
        backend._initialize_schema()
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_rejects_wrong_credentials() -> None:
    """A real authentication failure raises `BackendError`, the public Genie error."""

    assert _LIVE_URI is not None
    with pytest.raises(BackendError, match="authentication or authorization"):
        Neo4jBackend(
            _LIVE_URI,
            auth=("neo4j", "definitely-wrong-password"),
            database=os.environ.get("GENIE_NEO4J_DATABASE", "neo4j"),
            connection_timeout=1.0,
            max_transaction_retry_time=1.0,
        )
