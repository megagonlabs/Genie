"""Unit tests and optional live tests for the MongoDB backend that uses `pymongo`."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from time import sleep
from typing import Any, Literal

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
    MongoDBBackend,
    RouteCatalog,
    RouteInfo,
    Scope,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.document import mongodb as mongodb_module
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


def test_mongodb_backend_implements_every_backend_protocol() -> None:
    """`MongoDBBackend` implements `MemoryBackend` and every optional capability protocol."""

    backend = object.__new__(MongoDBBackend)

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
        ({"uri": "mongodb:///app", "database": 1}, TypeError, "database must be a string"),
        ({"uri": "mongodb:///app", "database": ""}, ValueError, "database must not be empty"),
        ({"uri": "mongodb:///app", "database": "genie memory"}, ValueError, "database must not contain"),
        ({"uri": "mongodb:///app", "database": "genie.memory"}, ValueError, "database must not contain"),
        ({"uri": "mongodb:///app", "database": "genie$memory"}, ValueError, "database must not contain"),
        ({"uri": "mongodb:///app", "database": "g" * 64}, ValueError, "database must be at most 63 bytes"),
        ({"uri": "mongodb:///app", "connect_timeout": True}, TypeError, "connect_timeout must be a number"),
        ({"uri": "mongodb:///app", "connect_timeout": 0}, ValueError, "connect_timeout must be a finite number greater than zero"),
        ({"uri": "mongodb:///app", "socket_timeout": -1}, ValueError, "socket_timeout must be a finite number"),
        ({"uri": "mongodb:///app", "max_retry_time": float("inf")}, ValueError, "max_retry_time must be a finite number"),
        ({"uri": "mongodb:///app", "auth": ("user",)}, TypeError, "auth must be a"),
        ({"uri": "mongodb:///app", "durability": 1}, TypeError, "durability must be a string"),
        ({"uri": "mongodb:///app", "durability": "fast"}, ValueError, "durability must be either"),
        ({"uri": "mongodb:///app", "verify_server_role": 1}, TypeError, "verify_server_role must be"),
        ({"uri": "mongodb:///app", "verify_transaction_support": 1}, TypeError, "verify_transaction_support must be"),
        ({"uri": "mongodb:///app", "initialize_schema": 1}, TypeError, "initialize_schema must be"),
    ],
)
def test_mongodb_backend_rejects_invalid_configuration_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """`MongoDBBackend` rejects an invalid configuration before it loads the driver, so it does not open a network connection."""

    monkeypatch.setattr(
        mongodb_module,
        "_load_pymongo",
        lambda: pytest.fail("driver should not be loaded"),
    )

    with pytest.raises(error, match=message):
        MongoDBBackend(**kwargs)


def test_mongodb_backend_explains_its_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the environment does not have the driver, `MongoDBBackend` raises `UnsupportedOperation` that names the `genie[mongodb]` extra."""

    def missing() -> tuple[Any, Any, Any, tuple[type[BaseException], ...]]:
        raise UnsupportedOperation("install 'genie[mongodb]'")

    monkeypatch.setattr(mongodb_module, "_load_pymongo", missing)

    with pytest.raises(UnsupportedOperation, match=r"genie\[mongodb\]"):
        MongoDBBackend("mongodb:///app")


def test_mongodb_serialization_is_strict_and_defensive() -> None:
    """The stored document form keeps the JSON value types of a block."""

    original = MemoryBlock(
        id="note",
        type="note",
        value={"items": [1, True, None], "ratio": 0.5},
        metadata={"nested": {"value": "kept"}},
        policy=MemoryPolicy.owner_private(),
        created_by="agent:writer",
    )

    document = mongodb_module._block_document(original)
    document["storage_order"] = 0

    restored = mongodb_module._block_from_document(document)

    assert restored == original
    assert restored.value["items"][1] is True
    assert isinstance(restored.value["items"][0], int)
    assert restored.policy == original.policy


def test_mongodb_keeps_caller_data_out_of_field_names() -> None:
    """The backend keeps a dotted key or a key with a `$` prefix inside stored JSON text, not in a field name."""

    block = MemoryBlock(id="note", type="note", value={"$set": 1, "a.b": 2})

    document = mongodb_module._block_document(block)

    assert set(document) == {
        "_id",
        "parent_id",
        "type",
        "value_json",
        "content_type",
        "metadata_json",
        "policy_json",
        "created_at",
        "updated_at",
        "created_by",
        "version",
    }
    assert isinstance(document["value_json"], str)
    document["storage_order"] = 0
    assert mongodb_module._block_from_document(document).value == {"$set": 1, "a.b": 2}


def test_mongodb_link_and_temporal_documents_round_trip() -> None:
    """A link and a temporal entry do not change after a round trip through the stored document encoding."""

    from genie.structures import ValidityInterval

    link = MemoryLink("source", "target", "supports", metadata={"weight": 2})
    entry = TemporalEntry(
        collection_id="timeline",
        block_id="event",
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        validity=ValidityInterval(valid_at=datetime(2024, 1, 2, tzinfo=UTC), invalid_at=datetime(2024, 1, 3, tzinfo=UTC)),
    )

    link_document = mongodb_module._link_document(link)
    link_document["storage_order"] = 0

    assert mongodb_module._link_from_document(link_document) == link
    assert mongodb_module._temporal_from_document(mongodb_module._temporal_document(entry)) == entry


def test_mongodb_route_entry_documents_round_trip() -> None:
    """A route catalog entry uses its block id as `_id`, and it does not change after a round trip."""

    entry = RouteInfo("mem_block", "eu")

    document = mongodb_module._route_entry_document(entry)

    assert document == {"_id": "mem_block", "route": "eu"}
    assert mongodb_module._route_entry_from_document(document) == entry


def test_mongodb_rejects_values_that_are_not_strict_json() -> None:
    """The backend raises `TypeError` for a value that cannot make a JSON round trip, before it writes the value."""

    with pytest.raises(TypeError, match="must be strict JSON data"):
        mongodb_module._dump_json({1: "integer key"}, field_name="block value")
    with pytest.raises(TypeError, match="must be strict JSON data"):
        mongodb_module._dump_json(float("nan"), field_name="block value")


def test_mongodb_reports_corrupt_stored_documents() -> None:
    """Stored data that the backend cannot read raises `BackendCorruption`, not a decode error."""

    with pytest.raises(BackendCorruption, match="JSON is invalid"):
        mongodb_module._load_json("{not json")
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        mongodb_module._decode_datetime("not a timestamp")
    with pytest.raises(BackendCorruption, match="timestamps must be timezone-aware"):
        mongodb_module._decode_datetime("2024-01-01T00:00:00")
    with pytest.raises(BackendCorruption, match="route catalog entry is invalid"):
        mongodb_module._route_entry_from_document({"_id": "mem_block"})
    with pytest.raises(BackendCorruption, match="route catalog entry is invalid"):
        mongodb_module._route_entry_from_document({"_id": 7, "route": "eu"})
    with pytest.raises(BackendCorruption, match="route catalog entry is invalid"):
        mongodb_module._route_entry_from_document({"_id": "mem_block", "route": " "})


def test_mongodb_identities_separate_neighbouring_records() -> None:
    """Two different field pairs cannot make the same composite `_id`."""

    assert mongodb_module._identity("a", "bc") != mongodb_module._identity("ab", "c")
    assert mongodb_module._identity("log", 1) == mongodb_module._identity("log", 1)


_OPERATOR = {"$ne": None}
_BINDING = KeyBinding(collection_id="mem_keys", address=MemoryAddress((), "key"), block_id="mem_block")


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("get_block", (_OPERATOR,), {}),
        ("delete_block", (_OPERATOR,), {}),
        ("children", (_OPERATOR,), {}),
        ("ancestors", (_OPERATOR,), {}),
        ("descendants", (_OPERATOR,), {}),
        ("contains", ("mem_root", _OPERATOR), {}),
        ("rebind_key", (_BINDING,), {"expected_block_id": _OPERATOR}),
        ("get_key", (_OPERATOR, MemoryAddress((), "key")), {}),
        ("list_keys", (_OPERATOR,), {}),
        ("unbind_key", ("mem_keys", MemoryAddress((), "key")), {"expected_block_id": _OPERATOR}),
        ("delete_key_bindings_for_block", (_OPERATOR,), {}),
        ("append_log_entry", ("mem_log", _OPERATOR), {}),
        ("get_log_entry", (_OPERATOR, 0), {}),
        ("list_log_entries", (_OPERATOR,), {}),
        ("delete_log_entries_for_block", (_OPERATOR,), {}),
        ("get_temporal_entry", ("mem_time", _OPERATOR), {}),
        ("list_temporal_entries", ("mem_time",), {"after_block_id": _OPERATOR}),
        ("remove_temporal_entry", (_OPERATOR, "mem_block"), {}),
        ("delete_temporal_entries_for_block", (_OPERATOR,), {}),
        ("query_temporal_entries", ("mem_time",), {"after_block_id": _OPERATOR}),
        ("list_links", (_OPERATOR,), {}),
        ("list_links", ("mem_block",), {"relation": _OPERATOR}),
        ("delete_links_for_block", (_OPERATOR,), {}),
        ("get_route", (_OPERATOR,), {}),
        ("release_routes", ([_OPERATOR],), {}),
        ("release_routes", (["mem_block", _OPERATOR],), {}),
    ],
)
def test_mongodb_rejects_non_string_ids_before_querying(
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """The backend rejects an id that is not a string before MongoDB can read it as an operator.

    If a call passes the check, it fails with `AttributeError` instead of
    `TypeError`, because the backend has no connection.
    """

    backend = object.__new__(MongoDBBackend)

    with pytest.raises(TypeError, match="must be a string"):
        getattr(backend, method)(*args, **kwargs)


def test_mongodb_chunking_bounds_every_command_document() -> None:
    """The backend splits a long id list into chunks, so one `$in` list cannot make a command document too large."""

    values = [str(index) for index in range(1201)]

    chunks = list(mongodb_module._chunked(values, mongodb_module._QUERY_CHUNK))

    assert [len(chunk) for chunk in chunks] == [500, 500, 201]
    assert [item for chunk in chunks for item in chunk] == values
    assert list(mongodb_module._chunked([], 10)) == []


def test_mongodb_schema_expectations_describe_one_consistent_schema() -> None:
    """Each index that Genie owns has a unique name and belongs to a collection that Genie owns."""

    names: set[str] = set()
    for collection, indexes in mongodb_module._SCHEMA_COLLECTIONS.items():
        assert collection.islower()
        for index in indexes:
            # Each index name is unique in the whole database, not only in its
            # own collection.
            assert index.name not in names, index.name
            assert index.keys, index.name
            assert len({field for field, _ in index.keys}) == len(index.keys), index.name
            assert all(direction in {1, -1} for _, direction in index.keys)
            names.add(index.name)
    # MongoDB adds `_id_` automatically, so Genie does not declare it.
    assert "_id_" not in names

    # The schema includes each control collection that the adapter writes to.
    for collection in (mongodb_module._SCHEMA_INFO, mongodb_module._GUARD, mongodb_module._COUNTERS):
        assert collection in mongodb_module._SCHEMA_COLLECTIONS


class _FakeMongoError(Exception):
    """Test double for a `pymongo` error that carries a server error code."""

    def __init__(self, code: int | None = None, message: str = "failed") -> None:
        super().__init__(message)
        self.code = code
        self.labels: set[str] = set()

    def has_error_label(self, label: str) -> bool:
        return label in self.labels


# A transport failure has no server error code, so the translator identifies
# it by its class name. For this reason, these test doubles use the exact
# class names of `pymongo` and `bson`.


class DuplicateKeyError(_FakeMongoError):
    """Test double for `pymongo.errors.DuplicateKeyError`."""


class NotPrimaryError(_FakeMongoError):
    """Test double for `pymongo.errors.NotPrimaryError`."""


class ServerSelectionTimeoutError(_FakeMongoError):
    """Test double for `pymongo.errors.ServerSelectionTimeoutError`."""


class DocumentTooLarge(_FakeMongoError):
    """Test double for `pymongo.errors.DocumentTooLarge`."""


class InvalidBSON(_FakeMongoError):
    """Test double for `bson.errors.InvalidBSON`."""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_FakeMongoError(11000), MemoryConflict),
        (DuplicateKeyError(None), MemoryConflict),
        (_FakeMongoError(112), BackendBusy),
        (_FakeMongoError(24), BackendBusy),
        (_FakeMongoError(50), BackendBusy),
        (_FakeMongoError(251), BackendBusy),
        (_FakeMongoError(13), BackendReadOnly),
        (_FakeMongoError(10107), BackendReadOnly),
        (NotPrimaryError(None), BackendReadOnly),
        (_FakeMongoError(10334), BackendCapacityExceeded),
        (_FakeMongoError(292), BackendCapacityExceeded),
        (DocumentTooLarge(None), BackendCapacityExceeded),
        (InvalidBSON(None), BackendCorruption),
        (_FakeMongoError(89), BackendUnavailable),
        (_FakeMongoError(11602), BackendUnavailable),
        (ServerSelectionTimeoutError(None), BackendUnavailable),
        (_FakeMongoError(2), BackendError),
    ],
)
def test_mongodb_translates_driver_failures_to_stable_errors(
    error: BaseException,
    expected: type[Exception],
) -> None:
    """The backend translates each mapped error code and driver error type to the documented Genie error."""

    assert type(mongodb_module._translate_mongodb_error(error)) is expected


@pytest.mark.parametrize(
    "description",
    [
        {"isWritablePrimary": True, "setName": "rs0"},
        {"ismaster": True, "setName": "rs0"},
        {"isWritablePrimary": True, "msg": "isdbgrid"},
        {"isWritablePrimary": None},
        {},
        None,
    ],
    ids=["primary", "older-primary", "mongos", "null", "missing-field", "no-reply"],
)
def test_mongodb_server_role_probe_passes_unless_a_refusal_is_proven(description: Any) -> None:
    """Construction continues unless the reply proves that the node does not accept writes."""

    object.__new__(MongoDBBackend)._verify_server_role(description)


@pytest.mark.parametrize(
    "description",
    [{"isWritablePrimary": False, "secondary": True, "setName": "rs0"}, {"ismaster": False, "secondary": True}],
    ids=["hello", "ismaster"],
)
def test_mongodb_server_role_probe_refuses_a_node_that_refuses_writes(description: Any) -> None:
    """The backend raises `UnsupportedOperation` with a clear message for a server that reports that it is not writable."""

    with pytest.raises(UnsupportedOperation, match="does not accept writes"):
        object.__new__(MongoDBBackend)._verify_server_role(description)


@pytest.mark.parametrize(
    "description",
    [
        {"isWritablePrimary": True, "setName": "rs0"},
        {"isWritablePrimary": True, "msg": "isdbgrid"},
        {"isWritablePrimary": False, "setName": "rs0"},
        {"setName": ""},
        {},
        None,
    ],
    ids=["replica-set", "mongos", "secondary", "empty-set-name", "missing-field", "no-reply"],
)
def test_mongodb_transaction_probe_passes_unless_standalone_is_proven(description: Any) -> None:
    """Only a reply that proves a standalone server stops construction."""

    object.__new__(MongoDBBackend)._verify_transaction_support(description)


def test_mongodb_transaction_probe_refuses_a_standalone_server() -> None:
    """The backend rejects a standalone `mongod` because it cannot run transactions."""

    with pytest.raises(UnsupportedOperation, match="standalone server"):
        object.__new__(MongoDBBackend)._verify_transaction_support({"isWritablePrimary": True})


class _ProbeAdmin:
    """Admin database that answers only the server description command."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.seen: list[str] = []

    def command(self, name: str) -> Any:
        self.seen.append(name)
        answer = self.answers.get(name, _FakeMongoError(59, "no such command"))
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _ProbeClient:
    """Client that exposes only the admin database used by the probe."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.admin = _ProbeAdmin(answers)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _probe_backend(answers: dict[str, Any]) -> tuple[MongoDBBackend, _ProbeClient]:
    """Return a backend that uses a probe client, together with that client."""

    from threading import Condition, RLock

    client = _ProbeClient(answers)
    backend = object.__new__(MongoDBBackend)
    backend._client = client
    backend._closing = False
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._exception_types = (_FakeMongoError,)
    return backend, client


def test_mongodb_server_description_falls_back_to_the_older_command() -> None:
    """If a server does not support `hello`, the backend describes it through `ismaster`."""

    backend, client = _probe_backend({"ismaster": {"ismaster": True, "setName": "rs0"}})

    assert backend._read_server_description() == {"ismaster": True, "setName": "rs0"}
    assert client.admin.seen == ["hello", "ismaster"]


def test_mongodb_server_description_is_unknown_when_both_commands_fail() -> None:
    """If both description commands fail, the backend treats the deployment as unknown and does not reject it."""

    backend, _ = _probe_backend({})

    assert backend._read_server_description() is None


def test_mongodb_server_description_lets_a_connection_failure_through() -> None:
    """For a deployment that it cannot reach, the backend raises the connection error and does not treat the deployment as unknown."""

    backend, _ = _probe_backend({"hello": ServerSelectionTimeoutError(None, "no server")})

    with pytest.raises(ServerSelectionTimeoutError):
        backend._read_server_description()


class _FakeCollection:
    """Collection that answers nothing, for construction-time tests."""

    def find_one(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeDatabase:
    """Database test double that returns empty collections."""

    def __getitem__(self, name: str) -> _FakeCollection:
        return _FakeCollection()


class _FakeConcern:
    """Test double for a `pymongo` read concern or write concern."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


def test_mongodb_backend_discards_client_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `_initialize_schema` fails with an unexpected error, the backend closes the new client before the error reaches the caller."""

    closed = Event()

    class FakeClient:
        def get_database(self, name: str, **kwargs: object) -> _FakeDatabase:
            return _FakeDatabase()

        def close(self) -> None:
            closed.set()

    class FakeReadPreference:
        PRIMARY = "primary"

    class FakeErrors:
        PyMongoError = _FakeMongoError
        DuplicateKeyError = DuplicateKeyError
        CollectionInvalid = _FakeMongoError
        OperationFailure = _FakeMongoError

    class FakeDriver:
        errors = FakeErrors
        ReadPreference = FakeReadPreference

        @staticmethod
        def MongoClient(uri: str, **options: object) -> FakeClient:
            return FakeClient()

    monkeypatch.setattr(
        mongodb_module,
        "_load_pymongo",
        lambda: (FakeDriver, _FakeConcern, _FakeConcern, (_FakeMongoError,)),
    )
    monkeypatch.setattr(
        MongoDBBackend,
        "_initialize_schema",
        lambda self: (_ for _ in ()).throw(RuntimeError("unexpected schema failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected schema failure"):
        MongoDBBackend("mongodb:///app", verify_server_role=False, verify_transaction_support=False)

    assert closed.is_set()


_LIVE_URI = os.environ.get("GENIE_MONGODB_URI")
_LIVE_DRIVER_AVAILABLE = importlib.util.find_spec("pymongo") is not None
_LIVE_REASON = "set GENIE_MONGODB_URI and install the mongodb extra to run live MongoDB tests"
_LIVE_DATABASE = os.environ.get("GENIE_MONGODB_DATABASE", "genie_test")


def _live_backend(
    *,
    initialize_schema: bool = True,
    durability: Literal["full", "normal"] = "full",
) -> MongoDBBackend:
    assert _LIVE_URI is not None
    return MongoDBBackend(
        _LIVE_URI,
        database=_LIVE_DATABASE,
        durability=durability,
        initialize_schema=initialize_schema,
    )


def _clear_live_data(backend: MongoDBBackend) -> None:
    # MongoDB does not cascade a delete through references, so this function
    # clears every owned collection. It also clears the counters, so each test
    # starts with the same storage order.
    for collection in (
        backend._blocks,
        backend._links,
        backend._acyclic_relations,
        backend._key_bindings,
        backend._log_counters,
        backend._log_entries,
        backend._temporal_entries,
        backend._route_entries,
        backend._counters,
    ):
        collection.delete_many({})


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestMongoDBStructureBackendContracts(
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
    """Run every reusable capability contract against a live MongoDB deployment."""

    def setup_method(self) -> None:
        self.backend = _live_backend()
        _clear_live_data(self.backend)

    def teardown_method(self) -> None:
        _clear_live_data(self.backend)
        self.backend.close()

    def make_backend(self) -> MongoDBBackend:
        return self.backend


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestMongoDBNormalDurabilityStructureBackendContracts(TestMongoDBStructureBackendContracts):
    """Run every capability contract against `durability="normal"`.

    - **A different pair of concerns.** `"normal"` acknowledges a write on the
      primary only (`w=1`) and reads with the `local` read concern. The base
      class uses the `"full"` default, so it does not test this path.
    - **The contracts check read-your-own-writes.** A `w=1` write is not
      majority-committed yet. A read under the wrong concern can miss it.
      Every contract writes a result and then reads it again.
    """

    def setup_method(self) -> None:
        """Open one backend that acknowledges writes on the primary only."""

        self.backend = _live_backend(durability="normal")
        _clear_live_data(self.backend)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
@pytest.mark.parametrize(
    ("durability", "write_concern", "read_concern", "transaction_read_concern"),
    [
        ("full", {"w": "majority", "j": True}, "majority", "snapshot"),
        ("normal", {"w": 1}, "local", "local"),
    ],
)
def test_live_mongodb_durability_selects_its_concerns(
    durability: Literal["full", "normal"],
    write_concern: dict[str, Any],
    read_concern: str,
    transaction_read_concern: str,
) -> None:
    """Each durability mode gives the driver the concerns that it documents."""

    backend = _live_backend(durability=durability)
    try:
        assert backend.durability == durability
        assert backend._write_concern.document == write_concern
        assert backend._read_concern.level == read_concern
        assert backend._transaction_read_concern.level == transaction_read_concern
        # Every command inherits its concerns from the database handle. This check
        # proves that the handle has the concerns, not only the backend attributes.
        assert backend._database_handle.write_concern.document == write_concern
        assert backend._database_handle.read_concern.level == read_concern
    finally:
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_normal_durability_reads_back_its_own_write() -> None:
    """A `w=1` write is visible to the `local` read that follows it."""

    backend = _live_backend(durability="normal")
    _clear_live_data(backend)
    try:
        block = backend.put_block(MemoryBlock(id="fast", type="note", value={"text": "primary only"}))

        assert backend.get_block(block.id) == block

        # A second connection also reads the commit. This proves that the commit
        # is visible without a reuse of the session that wrote it.
        reader = _live_backend(initialize_schema=False, durability="normal")
        try:
            assert reader.get_block(block.id) == block
        finally:
            reader.close()
    finally:
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_persists_and_coordinates_instances() -> None:
    """Committed state stays after a reconnect, and concurrent instances share the guard."""

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

        def add(backend: MongoDBBackend, link: MemoryLink) -> bool:
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


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_instances_claim_one_route_for_each_block_id() -> None:
    """Of 2 instances that claim one block id at the same time, each for its own route, only one succeeds, and both read its entry."""

    left = _live_backend()
    _clear_live_data(left)
    right = _live_backend(initialize_schema=False)
    entries = [(left, RouteInfo("mem_shared", "us")), (right, RouteInfo("mem_shared", "eu"))]
    barrier = Barrier(len(entries))

    def claim(backend: MongoDBBackend, entry: RouteInfo) -> bool:
        barrier.wait()
        try:
            backend.claim_route(entry)
        except MemoryConflict:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=len(entries)) as executor:
            outcomes = list(executor.map(lambda item: claim(*item), entries))

        assert outcomes.count(True) == 1
        winner = entries[outcomes.index(True)][1]
        assert left.get_route(winner.block_id) == winner
        assert right.get_route(winner.block_id) == winner
    finally:
        _clear_live_data(left)
        left.close()
        right.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_deletes_a_subtree_with_its_dependent_records() -> None:
    """One transaction deletes the subtree and every record that refers to it."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        root = backend.put_block(MemoryBlock(id="root", type="project", value={}))
        child = backend.put_block(MemoryBlock(id="child", type="note", value={}, parent_id=root.id))
        outside = backend.put_block(MemoryBlock(id="outside", type="note", value={}))
        backend.add_link(MemoryLink(outside.id, child.id, "mentions"))
        backend.append_log_entry(root.id, child.id)
        backend.add_temporal_entry(TemporalEntry(collection_id=root.id, block_id=child.id, occurred_at=datetime(2024, 1, 1, tzinfo=UTC)))

        backend.delete_block(root.id)

        assert backend.get_block(child.id) is None
        assert backend.list_links(outside.id, direction="both") == []
        assert backend.list_log_entries(root.id) == []
        assert backend.list_temporal_entries(root.id) == []
        assert backend.get_block(outside.id) == outside
    finally:
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_orders_text_by_bytes_not_by_locale() -> None:
    """Pages sort text by byte order, not by locale, so the order matches the other shipped adapters."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        collection = backend.put_block(MemoryBlock(id="collection", type="timeline", value={}))
        # A locale collation sorts these ids in a different order from plain
        # byte order.
        ids = ["a-1", "A-2", "a_3", "AB", "ab"]
        moment = datetime(2024, 1, 1, tzinfo=UTC)
        for block_id in ids:
            backend.put_block(MemoryBlock(id=block_id, type="event", value={}))
            backend.add_temporal_entry(TemporalEntry(collection_id=collection.id, block_id=block_id, occurred_at=moment))

        listed = [entry.block_id for entry in backend.list_temporal_entries(collection.id, limit=10)]

        assert listed == sorted(ids)
    finally:
        _clear_live_data(backend)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_serves_the_public_memory_api() -> None:
    """`Memory` can use the adapter for ownership and scoped search."""

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


_LIVE_INIT_DATABASE = f"{_LIVE_DATABASE}_init"


def _live_init_backend() -> MongoDBBackend:
    """Open a backend on the database reserved for initialization tests."""

    assert _LIVE_URI is not None
    return MongoDBBackend(_LIVE_URI, database=_LIVE_INIT_DATABASE)


def _drop_live_init_database() -> None:
    """Remove the initialization test database so the next test starts empty."""

    with _live_backend() as helper:
        helper._require_client().drop_database(_LIVE_INIT_DATABASE)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_coordinates_concurrent_initializers() -> None:
    """Constructors that start at almost the same time on one empty database all succeed.

    - **Staggered starts.** Each constructor starts a few milliseconds after the
      one before it, so most constructors read the database while another
      constructor creates collections.
    - **The old failure.** A check that did not read the record again failed in
      this time window.
    """

    _drop_live_init_database()

    def construct(index: int) -> MongoDBBackend:
        sleep(index * 0.02)
        return _live_init_backend()

    with ThreadPoolExecutor(max_workers=8) as executor:
        backends = list(executor.map(construct, range(8)))
    try:
        for backend in backends:
            backend._validate_schema()
    finally:
        for backend in backends:
            backend.close()
        _drop_live_init_database()


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_writes_its_schema_record_before_other_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend writes its schema record at version 0 before the first migration creates any other collection."""

    _drop_live_init_database()
    seen: list[tuple[Any, set[str]]] = []
    original = MongoDBBackend._apply_schema_migration

    def observe(self: MongoDBBackend, target_version: int) -> None:
        seen.append((self._read_schema_identity(), self._database_collections()))
        original(self, target_version)

    monkeypatch.setattr(MongoDBBackend, "_apply_schema_migration", observe)
    try:
        with _live_init_backend():
            pass
    finally:
        _drop_live_init_database()

    assert len(seen) == 1
    state, collections = seen[0]
    assert state is not None
    assert state["application_id"] == mongodb_module._APPLICATION_ID
    assert state["version"] == 0
    assert collections == {mongodb_module._SCHEMA_INFO}


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_refuses_a_database_with_foreign_collections() -> None:
    """The backend rejects a database with collections that no Genie record owns, and it does not change them."""

    _drop_live_init_database()
    with _live_backend() as helper:
        foreign = helper._require_client()[_LIVE_INIT_DATABASE]
        foreign["notes"].insert_one({"_id": 1})
        try:
            with pytest.raises(UnsupportedOperation, match="no Genie schema record"):
                _live_init_backend()
            assert foreign.list_collection_names() == ["notes"]
        finally:
            helper._require_client().drop_database(_LIVE_INIT_DATABASE)


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
def test_live_mongodb_backend_resumes_a_database_claimed_at_version_zero() -> None:
    """The backend finishes the initialization of a database that has only a schema record at version 0."""

    _drop_live_init_database()
    with _live_backend() as helper:
        claimed = helper._require_client()[_LIVE_INIT_DATABASE]
        claimed[mongodb_module._SCHEMA_INFO].insert_one(
            {"_id": mongodb_module._SCHEMA_DOCUMENT_ID, "application_id": mongodb_module._APPLICATION_ID, "version": 0},
        )
        try:
            with _live_init_backend() as resumed:
                state = resumed._read_schema_identity()
                assert state is not None
                assert state["version"] == mongodb_module._SCHEMA_VERSION
        finally:
            helper._require_client().drop_database(_LIVE_INIT_DATABASE)
