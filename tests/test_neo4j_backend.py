"""Unit and opt-in live tests for the official-driver Neo4j backend."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import os
from threading import RLock
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
    Neo4jBackend,
    SequenceStore,
    Scope,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.graph import neo4j as neo4j_module
from tests.structures.test_backend_contracts import (
    AcyclicLinkStoreContract,
    BlockStoreContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalIndexContract,
    TemporalStoreContract,
)


def test_neo4j_backend_implements_every_backend_protocol() -> None:
    """The class exposes the complete core and optional capability surface."""

    backend = object.__new__(Neo4jBackend)

    assert isinstance(backend, MemoryBackend)
    assert isinstance(backend, AcyclicLinkStore)
    assert isinstance(backend, KeyedStore)
    assert isinstance(backend, SequenceStore)
    assert isinstance(backend, TemporalStore)
    assert isinstance(backend, TemporalIndex)


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
    """Invalid public configuration never attempts a network connection."""

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
    """Missing driver installations produce an actionable public error."""

    def missing() -> tuple[Any, tuple[type[BaseException], ...]]:
        raise UnsupportedOperation("install 'genie[neo4j]'")

    monkeypatch.setattr(neo4j_module, "_load_neo4j_driver", missing)

    with pytest.raises(UnsupportedOperation, match=r"genie\[neo4j\]"):
        Neo4jBackend("neo4j://example")


def test_neo4j_serialization_is_strict_and_defensive() -> None:
    """The graph property representation preserves JSON value types."""

    original = MemoryBlock(
        id="note",
        type="note",
        value={"items": [1, True, None]},
        metadata={"nested": {"value": "kept"}},
    )
    properties = neo4j_module._block_properties(original)
    reconstructed = neo4j_module._block_from_record(
        {"block": {**properties, "storage_order": 0}, "parent_id": None}
    )

    assert reconstructed == original
    reconstructed.value["items"].append("local")
    assert neo4j_module._block_from_record(
        {"block": {**properties, "storage_order": 0}, "parent_id": None}
    ).value == {"items": [1, True, None]}
    with pytest.raises(TypeError, match="strict JSON"):
        neo4j_module._block_properties(MemoryBlock(type="invalid", value=(1, 2)))


def test_neo4j_serialization_reports_corrupt_stored_values() -> None:
    """Malformed database properties fail as corruption, not incidental parser errors."""

    properties = neo4j_module._block_properties(MemoryBlock(id="note", type="note", value={}))
    properties["value_json"] = "{not-json"

    with pytest.raises(BackendCorruption, match="JSON"):
        neo4j_module._block_from_record(
            {"block": {**properties, "storage_order": 0}, "parent_id": None}
        )


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
    """Driver-specific failure details do not leak through the backend API."""

    class DriverFailure(Exception):
        def __init__(self) -> None:
            super().__init__(message)
            self.code = code

        def is_retryable(self) -> bool:
            return False

    assert isinstance(neo4j_module._translate_neo4j_error(DriverFailure()), expected)


def test_neo4j_backend_close_is_idempotent_and_enforces_lifecycle() -> None:
    """Closing releases the owned driver and rejects later operations."""

    class FakeDriver:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    driver = FakeDriver()
    backend = object.__new__(Neo4jBackend)
    backend._driver = driver
    backend._lock = RLock()
    backend._exception_types = ()

    backend.close()
    backend.close()

    assert driver.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        backend.get_block("missing")


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


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestNeo4jStructureBackendContracts(
    BlockStoreContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    AcyclicLinkStoreContract,
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
    """Committed state survives reconnects and concurrent instances share locks."""

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
    finally:
        _clear_live_data(left)
        left.close()
        right.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_preserves_link_identity_and_rolls_back_conflicts() -> None:
    """Failed topology changes are atomic and link metadata remains isolated."""

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
def test_live_neo4j_backend_matches_core_ownership_search_and_delete_behavior() -> None:
    """Ownership, scoped search, policies, and recursive cleanup match core behavior."""

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
def test_live_neo4j_backend_rejects_stale_writes_across_instances() -> None:
    """Database-resident locking protects CAS and subtree snapshots across drivers."""

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
            first.delete_block(root.id, expected_versions={root.id: current.version})
        assert first.get_block(child.id) == child
    finally:
        _clear_live_data(first)
        first.close()
        second.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_neo4j_backend_rejects_future_and_incomplete_schema() -> None:
    """Runtime instances reject unsupported versions and missing owned indexes."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
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

        backend._schema_statement("DROP INDEX genie_block_type_idx IF EXISTS")
        with pytest.raises(UnsupportedOperation, match="missing or incompatible"):
            _live_backend(initialize_schema=False)
    finally:
        backend._initialize_schema()
        _clear_live_data(backend)
        backend.close()
