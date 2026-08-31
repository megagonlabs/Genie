"""Unit and opt-in live tests for the pymongo MongoDB backend."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
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
    MongoDBBackend,
    Scope,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.document import mongodb as mongodb_module
from genie.structures import TemporalEntry
from tests.backends.test_backend_contracts import (
    AcyclicLinkStoreContract,
    BlockStoreContract,
    KeyedStoreContract,
    LinkStoreContract,
    SequenceStoreContract,
    TemporalIndexContract,
    TemporalStoreContract,
)


def test_mongodb_backend_implements_every_backend_protocol() -> None:
    """The class exposes the complete core and optional capability surface."""

    backend = object.__new__(MongoDBBackend)

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
    """Invalid public configuration never attempts a network connection."""

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
    """Missing driver installations produce an actionable public error."""

    def missing() -> tuple[Any, Any, Any, tuple[type[BaseException], ...]]:
        raise UnsupportedOperation("install 'genie[mongodb]'")

    monkeypatch.setattr(mongodb_module, "_load_pymongo", missing)

    with pytest.raises(UnsupportedOperation, match=r"genie\[mongodb\]"):
        MongoDBBackend("mongodb:///app")


def test_mongodb_serialization_is_strict_and_defensive() -> None:
    """The stored document representation preserves JSON value types."""

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
    """A dotted or dollar-prefixed key stays inside stored JSON text."""

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
    """Links and temporal entries survive the stored document encoding."""

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


def test_mongodb_rejects_values_that_are_not_strict_json() -> None:
    """A value that cannot round-trip through JSON is refused on the way in."""

    with pytest.raises(TypeError, match="must be strict JSON data"):
        mongodb_module._dump_json({1: "integer key"}, field_name="block value")
    with pytest.raises(TypeError, match="must be strict JSON data"):
        mongodb_module._dump_json(float("nan"), field_name="block value")


def test_mongodb_reports_corrupt_stored_documents() -> None:
    """Unreadable stored data raises a corruption error, not a decode error."""

    with pytest.raises(BackendCorruption, match="JSON is invalid"):
        mongodb_module._load_json("{not json")
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        mongodb_module._decode_datetime("not a timestamp")
    with pytest.raises(BackendCorruption, match="timestamps must be timezone-aware"):
        mongodb_module._decode_datetime("2024-01-01T00:00:00")


def test_mongodb_identities_separate_neighbouring_records() -> None:
    """A composite `_id` cannot be produced by two different field pairs."""

    assert mongodb_module._identity("a", "bc") != mongodb_module._identity("ab", "c")
    assert mongodb_module._identity("log", 1) == mongodb_module._identity("log", 1)


def test_mongodb_chunking_bounds_every_command_document() -> None:
    """Long id lists are split so one `$in` list cannot outgrow a command."""

    values = [str(index) for index in range(1201)]

    chunks = list(mongodb_module._chunked(values, mongodb_module._QUERY_CHUNK))

    assert [len(chunk) for chunk in chunks] == [500, 500, 201]
    assert [item for chunk in chunks for item in chunk] == values
    assert list(mongodb_module._chunked([], 10)) == []


def test_mongodb_schema_expectations_describe_one_consistent_schema() -> None:
    """Every owned index is named once and indexes an owned collection."""

    names: set[str] = set()
    for collection, indexes in mongodb_module._SCHEMA_COLLECTIONS.items():
        assert collection.islower()
        for index in indexes:
            # An index name must be unique across the database, not only
            # inside its own collection.
            assert index.name not in names, index.name
            assert index.keys, index.name
            assert len({field for field, _ in index.keys}) == len(index.keys), index.name
            assert all(direction in {1, -1} for _, direction in index.keys)
            names.add(index.name)
    # MongoDB adds `_id_` on its own, so Genie must never declare it.
    assert "_id_" not in names

    # The control collections the adapter writes to must be part of the schema.
    for collection in (mongodb_module._SCHEMA_INFO, mongodb_module._GUARD, mongodb_module._COUNTERS):
        assert collection in mongodb_module._SCHEMA_COLLECTIONS


class _FakeMongoError(Exception):
    """Stand-in for a pymongo error carrying a server error code."""

    def __init__(self, code: int | None = None, message: str = "failed") -> None:
        super().__init__(message)
        self.code = code
        self.labels: set[str] = set()

    def has_error_label(self, label: str) -> bool:
        return label in self.labels


# A transport failure carries no server error code, so the translator
# recognizes it by class name. These stand-ins therefore use the exact names
# `pymongo` and `bson` give their own classes.


class DuplicateKeyError(_FakeMongoError):
    """Stand-in for `pymongo.errors.DuplicateKeyError`."""


class NotPrimaryError(_FakeMongoError):
    """Stand-in for `pymongo.errors.NotPrimaryError`."""


class ServerSelectionTimeoutError(_FakeMongoError):
    """Stand-in for `pymongo.errors.ServerSelectionTimeoutError`."""


class DocumentTooLarge(_FakeMongoError):
    """Stand-in for `pymongo.errors.DocumentTooLarge`."""


class InvalidBSON(_FakeMongoError):
    """Stand-in for `bson.errors.InvalidBSON`."""


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
    """Every mapped error code and driver type becomes the documented error."""

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
    """Anything short of a proven non-writable node lets construction continue."""

    object.__new__(MongoDBBackend)._verify_server_role(description)


@pytest.mark.parametrize(
    "description",
    [{"isWritablePrimary": False, "secondary": True, "setName": "rs0"}, {"ismaster": False, "secondary": True}],
    ids=["hello", "ismaster"],
)
def test_mongodb_server_role_probe_refuses_a_node_that_refuses_writes(description: Any) -> None:
    """A server that reports it is not writable is refused with a clear message."""

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
    """A standalone `mongod` cannot run transactions, so it is refused."""

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


def _probe_backend(answers: dict[str, Any]) -> MongoDBBackend:
    from threading import Condition, RLock

    backend = object.__new__(MongoDBBackend)
    backend._client = _ProbeClient(answers)
    backend._closing = False
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._exception_types = (_FakeMongoError,)
    return backend


def test_mongodb_server_description_falls_back_to_the_older_command() -> None:
    """A server without `hello` is still described through `ismaster`."""

    backend = _probe_backend({"ismaster": {"ismaster": True, "setName": "rs0"}})

    assert backend._read_server_description() == {"ismaster": True, "setName": "rs0"}
    assert backend._client.admin.seen == ["hello", "ismaster"]


def test_mongodb_server_description_is_unknown_when_both_commands_fail() -> None:
    """A blocked command leaves the deployment unknown instead of refused."""

    backend = _probe_backend({})

    assert backend._read_server_description() is None


def test_mongodb_server_description_lets_a_connection_failure_through() -> None:
    """An unreachable deployment is reported instead of treated as unknown."""

    backend = _probe_backend({"hello": ServerSelectionTimeoutError(None, "no server")})

    with pytest.raises(ServerSelectionTimeoutError):
        backend._read_server_description()


class _FakeCollection:
    """Collection that answers nothing, for construction-time tests."""

    def find_one(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeDatabase:
    """Database that hands out empty collections."""

    def __getitem__(self, name: str) -> _FakeCollection:
        return _FakeCollection()


class _FakeConcern:
    """Stand-in for a pymongo read or write concern."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


def test_mongodb_backend_discards_client_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected schema failure closes the new client before escaping."""

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


def _live_backend(*, initialize_schema: bool = True) -> MongoDBBackend:
    assert _LIVE_URI is not None
    return MongoDBBackend(
        _LIVE_URI,
        database=_LIVE_DATABASE,
        initialize_schema=initialize_schema,
    )


def _clear_live_data(backend: MongoDBBackend) -> None:
    # MongoDB has no cascading reference, so every owned collection is cleared.
    # The counters go too, which keeps storage order deterministic per test.
    for collection in (
        backend._blocks,
        backend._links,
        backend._acyclic_relations,
        backend._key_bindings,
        backend._log_counters,
        backend._log_entries,
        backend._temporal_entries,
        backend._counters,
    ):
        collection.delete_many({})


@pytest.mark.skipif(not (_LIVE_URI and _LIVE_DRIVER_AVAILABLE), reason=_LIVE_REASON)
class TestMongoDBStructureBackendContracts(
    BlockStoreContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
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
def test_live_mongodb_backend_persists_and_coordinates_instances() -> None:
    """Committed state survives reconnects and concurrent instances share the guard."""

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
def test_live_mongodb_deletes_a_subtree_with_its_dependent_records() -> None:
    """One transaction removes the subtree and everything that referenced it."""

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
    """Paging uses byte order, so it matches the other shipped adapters."""

    backend = _live_backend()
    _clear_live_data(backend)
    try:
        collection = backend.put_block(MemoryBlock(id="collection", type="timeline", value={}))
        # Under a locale collation these ids sort in a different order than
        # plain byte order puts them in.
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
