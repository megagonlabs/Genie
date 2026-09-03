"""Unit and opt-in live tests for the official-client Redis backend."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Condition, Event, RLock
from time import monotonic, sleep
from types import SimpleNamespace
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
    RedisBackend,
    Scope,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
    UnsupportedOperation,
)
from genie.backend.key_value import redis as redis_module
from genie.structures import KeyBinding, MemoryAddress, TemporalEntry
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


class _FakeWatchError(Exception):
    """Stand-in for the client's optimistic conflict error."""


class _FakeNoScriptError(Exception):
    """Stand-in for the client's missing cached script error."""


class _FakeConnectionError(Exception):
    """Stand-in for the client's dropped connection error."""


class _FakeBackoff:
    """Stand-in for the client's exponential backoff."""


class _FakeRetry:
    """Stand-in for the client's retry policy."""

    def __init__(self, backoff: object, retries: int) -> None:
        self.backoff = backoff
        self.retries = retries


class _FakeClientModule:
    """Stand-in for the parts of the `redis` package the backend reads."""

    retry = SimpleNamespace(Retry=_FakeRetry)
    backoff = SimpleNamespace(ExponentialBackoff=_FakeBackoff)
    exceptions = SimpleNamespace(ConnectionError=_FakeConnectionError)


def test_redis_backend_implements_every_backend_protocol() -> None:
    """The class exposes the complete core and optional capability surface."""

    backend = object.__new__(RedisBackend)

    assert isinstance(backend, MemoryBackend)
    assert isinstance(backend, AcyclicLinkStore)
    assert isinstance(backend, KeyedStore)
    assert isinstance(backend, SequenceStore)
    assert isinstance(backend, TemporalStore)
    assert isinstance(backend, TemporalIndex)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"url": 1}, TypeError, "url must be a string"),
        ({"url": ""}, ValueError, "url must not be empty"),
        ({"url": "redis://example", "key_prefix": 1}, TypeError, "key_prefix must be a string"),
        ({"url": "redis://example", "key_prefix": ""}, ValueError, "key_prefix must not be empty"),
        ({"url": "redis://example", "key_prefix": "genie keys"}, ValueError, "key_prefix must not contain whitespace"),
        ({"url": "redis://example", "connect_timeout": True}, TypeError, "connect_timeout must be a number"),
        ({"url": "redis://example", "connect_timeout": -1}, ValueError, "connect_timeout must be a finite number"),
        ({"url": "redis://example", "socket_timeout": float("inf")}, ValueError, "socket_timeout must be a finite number"),
        ({"url": "redis://example", "max_retry_time": "5"}, TypeError, "max_retry_time must be a number"),
        ({"url": "redis://example", "health_check_interval": "5"}, TypeError, "health_check_interval must be a number"),
        ({"url": "redis://example", "health_check_interval": -1}, ValueError, "health_check_interval must be a finite number"),
        ({"url": "redis://example", "transport_retries": 1.5}, TypeError, "transport_retries must be an integer"),
        ({"url": "redis://example", "transport_retries": True}, TypeError, "transport_retries must be an integer"),
        ({"url": "redis://example", "transport_retries": -1}, ValueError, "transport_retries must be greater than or equal to zero"),
        ({"url": "redis://example", "auth": ["user", "password"]}, TypeError, "auth must be"),
        ({"url": "redis://example", "verify_connectivity": 1}, TypeError, "verify_connectivity must be"),
        ({"url": "redis://example", "verify_server_role": 1}, TypeError, "verify_server_role must be"),
        ({"url": "redis://example", "verify_eviction_policy": 1}, TypeError, "verify_eviction_policy must be"),
        ({"url": "redis://example", "initialize_schema": 1}, TypeError, "initialize_schema must be"),
    ],
)
def test_redis_backend_rejects_invalid_configuration_before_loading_client(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """Invalid public configuration never attempts a network connection."""

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: pytest.fail("client should not be loaded"),
    )

    with pytest.raises(error, match=message):
        RedisBackend(**kwargs)


def test_redis_backend_explains_its_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing client installations produce an actionable public error."""

    def missing() -> tuple[Any, tuple[type[BaseException], ...], type[BaseException], type[BaseException]]:
        raise UnsupportedOperation("install 'genie[redis]'")

    monkeypatch.setattr(redis_module, "_load_redis_client", missing)

    with pytest.raises(UnsupportedOperation, match=r"genie\[redis\]"):
        RedisBackend("redis://example")


def test_redis_serialization_is_strict_and_defensive() -> None:
    """The stored document representation preserves JSON value types."""

    original = MemoryBlock(
        id="note",
        type="note",
        value={"items": [1, True, None]},
        metadata={"nested": {"value": "kept"}},
    )
    document = {**redis_module._encode_block(original), "storage_order": 0}
    payload = redis_module._dump_json(document, field_name="memory block")
    reconstructed = redis_module._block_from_document(redis_module._load_document(payload, name="memory block"))

    assert reconstructed == original
    reconstructed.value["items"].append("local")
    assert redis_module._block_from_document(redis_module._load_document(payload, name="memory block")).value == {"items": [1, True, None]}
    with pytest.raises(TypeError, match="strict JSON"):
        redis_module._encode_block(MemoryBlock(type="invalid", value=(1, 2)))


def test_redis_serialization_reports_corrupt_stored_values() -> None:
    """Malformed stored documents fail as corruption, not parser errors."""

    with pytest.raises(BackendCorruption, match="memory block JSON is invalid"):
        redis_module._load_document("{not-json", name="memory block")
    with pytest.raises(BackendCorruption, match="not strict JSON"):
        redis_module._load_document("NaN", name="memory block")
    with pytest.raises(BackendCorruption, match="must be JSON objects"):
        redis_module._load_document("[]", name="memory block")
    with pytest.raises(BackendCorruption, match="documents must be strings"):
        redis_module._load_document(1, name="memory block")


def test_redis_serialization_rejects_invalid_versions_times_and_namespaces() -> None:
    """Malformed caller values and stored primitives fail at stable boundaries."""

    with pytest.raises(ValueError, match="block version"):
        redis_module._encode_block(MemoryBlock(id="note", type="note", value={}, version=0))
    with pytest.raises(TypeError, match="created_at must be a datetime"):
        redis_module._encode_datetime(cast(datetime, "bad"), field_name="created_at")
    with pytest.raises(ValueError, match="created_at must be timezone-aware"):
        redis_module._encode_datetime(
            datetime(2026, 1, 1),  # noqa: DTZ001 - deliberately malformed input
            field_name="created_at",
        )
    with pytest.raises(BackendCorruption, match="timestamps must be strings"):
        redis_module._decode_datetime(1)
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        redis_module._decode_datetime("not-a-timestamp")
    with pytest.raises(BackendCorruption, match="timezone-aware"):
        redis_module._decode_datetime("2026-01-01T00:00:00")
    with pytest.raises(BackendCorruption, match="key namespaces must be string lists"):
        redis_module._decode_address('[{},"key"]')
    with pytest.raises(BackendCorruption, match="key names must be strings"):
        redis_module._decode_address('[[],1]')
    with pytest.raises(BackendCorruption, match="block version must be an integer"):
        redis_module._stored_int("one", field_name="block version")
    with pytest.raises(BackendCorruption, match="block version must be an integer"):
        redis_module._stored_int(True, field_name="block version")
    assert redis_module._stored_int(None, field_name="block version") == 0


@pytest.mark.parametrize(
    ("decoder", "stored", "message"),
    [
        (redis_module._decode_address, "{}", "key address is invalid"),
        (redis_module._decode_binding_reference, '[1,"address"]', "key binding reference is invalid"),
        (redis_module._decode_log_member, '["one","block"]', "log entry is invalid"),
        (redis_module._decode_log_reference, '[1,2]', "log entry reference is invalid"),
        (redis_module._decode_edge_member, '["link",1,2]', "link relation edge is invalid"),
        (redis_module._decode_endpoint_member, '["link","source","target",1]', "link endpoint member is invalid"),
        (redis_module._decode_endpoint_member, '["link","source","target"]', "link endpoint member is invalid"),
    ],
)
def test_redis_member_decoders_translate_malformed_shapes(
    decoder: Any,
    stored: str,
    message: str,
) -> None:
    """Every stored composite identity maps malformed shapes to corruption."""

    with pytest.raises(BackendCorruption, match=message):
        decoder(stored)


def test_redis_document_decoders_translate_malformed_shapes() -> None:
    """Stored block, link, and temporal documents report their own corruption."""

    with pytest.raises(BackendCorruption, match="memory block is invalid"):
        redis_module._block_from_document({})
    with pytest.raises(BackendCorruption, match="memory link is invalid"):
        redis_module._link_from_document({})
    with pytest.raises(BackendCorruption, match="temporal entry is invalid"):
        redis_module._temporal_from_payload("collection", "block", "{}")
    with pytest.raises(BackendCorruption, match="timestamp is invalid"):
        redis_module._temporal_from_payload("collection", "block", '{"occurred_at":"invalid"}')

    document = {**redis_module._encode_block(MemoryBlock(id="note", type="note", value={}, policy=MemoryPolicy.public())), "storage_order": 0}
    document["policy"]["read"]["allow_all"] = "false"
    with pytest.raises(BackendCorruption, match="memory block is invalid"):
        redis_module._block_from_document(document)

    document["metadata"] = []
    with pytest.raises(BackendCorruption, match="memory block is invalid"):
        redis_module._block_from_document(document)


@pytest.mark.parametrize(
    ("class_name", "message", "expected"),
    [
        ("ClusterCrossSlotError", "CROSSSLOT Keys in request don't hash to the same slot", UnsupportedOperation),
        ("ResponseError", "MOVED 3999 127.0.0.1:6381", UnsupportedOperation),
        ("ResponseError", "ASK 3999 127.0.0.1:6381", UnsupportedOperation),
        ("ResponseError", "ask the operator to enable scripting", BackendError),
        ("OutOfMemoryError", "OOM command not allowed when used memory > 'maxmemory'", BackendCapacityExceeded),
        ("ReadOnlyError", "READONLY You can't write against a read only replica", BackendReadOnly),
        ("AuthenticationError", "WRONGPASS invalid username-password pair", BackendError),
        ("InvalidResponse", "protocol error", BackendCorruption),
        ("TimeoutError", "Timeout reading from socket", BackendBusy),
        ("BusyLoadingError", "Redis is loading the dataset in memory", BackendUnavailable),
        ("ResponseError", "MISCONF Redis is unable to persist to disk", BackendUnavailable),
        ("ConnectionError", "Error 61 connecting to localhost:6379", BackendUnavailable),
        ("ResponseError", "unknown command 'GENIE'", BackendError),
    ],
)
def test_redis_client_errors_map_to_public_errors(
    class_name: str,
    message: str,
    expected: type[Exception],
) -> None:
    """Client-specific failure details do not leak through the backend API."""

    failure = type(class_name, (Exception,), {})(message)

    assert isinstance(redis_module._translate_redis_error(failure), expected)


def test_redis_cluster_replies_name_the_unsupported_deployment() -> None:
    """A cluster reply reports a wrong target instead of a retryable failure."""

    failure = type("ResponseError", (Exception,), {})("MOVED 3999 127.0.0.1:6381")
    translated = redis_module._translate_redis_error(failure)

    assert isinstance(translated, UnsupportedOperation)
    assert "Redis Cluster" in str(translated)
    assert "standalone" in str(translated)


def test_redis_backend_close_is_idempotent_and_enforces_lifecycle() -> None:
    """Closing releases the owned client and rejects later operations."""

    class FakeClient:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    client = FakeClient()
    backend = _detached_backend(client)

    backend.close()
    backend.close()

    assert client.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        backend.get_block("missing")
    with pytest.raises(RuntimeError, match="closed"):
        backend.__enter__()


def test_redis_backend_close_waits_for_active_operations() -> None:
    """Closing never races an operation that already leased the client."""

    operation_started = Event()
    release_operation = Event()
    client_closed = Event()

    class FakeClient:
        def get(self, key: str) -> None:
            operation_started.set()
            assert release_operation.wait(timeout=2)

        def close(self) -> None:
            client_closed.set()

    backend = _detached_backend(FakeClient())

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation = executor.submit(backend.get_block, "missing")
        assert operation_started.wait(timeout=2)
        closing = executor.submit(backend.close)
        with backend._lifecycle:
            assert backend._lifecycle.wait_for(lambda: backend._closing, timeout=2)
        assert not client_closed.is_set()
        release_operation.set()
        assert operation.result(timeout=2) is None
        closing.result(timeout=2)

    assert client_closed.is_set()


def test_redis_backend_translates_client_close_failures() -> None:
    """Client shutdown failures clear lifecycle state and use public errors."""

    class ClientFailure(Exception):
        pass

    class FakeClient:
        def close(self) -> None:
            raise ClientFailure("Error 61 connecting to localhost:6379")

    backend = _detached_backend(FakeClient(), exception_types=(ClientFailure,))

    with pytest.raises(BackendUnavailable, match="unavailable"):
        backend.close()

    assert backend._client is None
    assert backend._closing is False


def test_redis_backend_forwards_production_client_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection, timeout, decoding, and auth settings reach the client."""

    calls: dict[str, Any] = {}

    class FakeClient:
        def ping(self) -> bool:
            calls["pinged"] = True
            return True

        def close(self) -> None:
            calls["closed"] = True

    class FakeRedis:
        @staticmethod
        def from_url(url: str, **options: object) -> FakeClient:
            calls.update(url=url, options=options)
            return FakeClient()

    class FakeModule(_FakeClientModule):
        Redis = FakeRedis

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: (FakeModule, (), _FakeWatchError, _FakeNoScriptError),
    )
    monkeypatch.setattr(RedisBackend, "_initialize_schema", lambda self: None)

    backend = RedisBackend(
        "rediss://example.test:6379/2",
        auth=("genie", "secret"),
        connect_timeout=4.5,
        socket_timeout=0.0,
        health_check_interval=15.0,
        transport_retries=2,
    )
    backend.close()

    options = cast(dict[str, Any], calls.pop("options"))
    retry = options.pop("retry")

    assert calls == {"url": "rediss://example.test:6379/2", "pinged": True, "closed": True}
    assert options == {
        "decode_responses": True,
        "socket_connect_timeout": 4.5,
        "socket_timeout": None,
        "health_check_interval": 15.0,
        "retry_on_error": [_FakeConnectionError],
        "username": "genie",
        "password": "secret",
    }
    assert isinstance(retry, _FakeRetry)
    assert isinstance(retry.backoff, _FakeBackoff)
    assert retry.retries == 2


def test_redis_backend_can_disable_the_health_check_and_transport_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero for both settings leaves the client at its own defaults."""

    calls: dict[str, Any] = {}

    class FakeClient:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            pass

    class FakeRedis:
        @staticmethod
        def from_url(url: str, **options: object) -> FakeClient:
            calls.update(url=url, options=options)
            return FakeClient()

    class FakeModule(_FakeClientModule):
        Redis = FakeRedis

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: (FakeModule, (), _FakeWatchError, _FakeNoScriptError),
    )
    monkeypatch.setattr(RedisBackend, "_initialize_schema", lambda self: None)

    backend = RedisBackend("redis://example", health_check_interval=0.0, transport_retries=0)
    backend.close()

    options = cast(dict[str, Any], calls["options"])

    assert options["health_check_interval"] == 0.0
    assert "retry" not in options
    assert "retry_on_error" not in options


@pytest.mark.parametrize(
    ("verify_eviction_policy", "expected_sections"),
    [(True, ["memory"]), (False, [])],
)
def test_redis_backend_checks_the_eviction_policy_before_it_touches_the_schema(
    monkeypatch: pytest.MonkeyPatch,
    verify_eviction_policy: bool,
    expected_sections: list[str],
) -> None:
    """The eviction check runs before schema work, and the flag can skip it.

    The role check is off here so the recorded sections cover eviction only.
    """

    steps: list[str] = []
    sections: list[str] = []

    class EvictingClient:
        def ping(self) -> bool:
            return True

        def info(self, section: str) -> dict[str, Any]:
            sections.append(section)
            return {"maxmemory": 1073741824, "maxmemory_policy": "allkeys-lru"}

        def close(self) -> None:
            return None

    class FakeRedis:
        @staticmethod
        def from_url(*args: object, **kwargs: object) -> EvictingClient:
            return EvictingClient()

    class FakeModule(_FakeClientModule):
        Redis = FakeRedis

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: (FakeModule, (), _FakeWatchError, _FakeNoScriptError),
    )
    monkeypatch.setattr(RedisBackend, "_initialize_schema", lambda self: steps.append("schema"))
    monkeypatch.setattr(RedisBackend, "_detect_readonly_scripts", lambda self: False)

    if verify_eviction_policy:
        with pytest.raises(UnsupportedOperation, match="configured to evict keys"):
            RedisBackend("redis://example", verify_server_role=False, verify_eviction_policy=True)
        assert steps == []
    else:
        backend = RedisBackend("redis://example", verify_server_role=False, verify_eviction_policy=False)
        backend.close()
        assert steps == ["schema"]
    assert sections == expected_sections


@pytest.mark.parametrize(
    ("verify_server_role", "expected_sections"),
    [(True, ["replication"]), (False, ["memory"])],
)
def test_redis_backend_checks_the_server_role_before_eviction_and_schema(
    monkeypatch: pytest.MonkeyPatch,
    verify_server_role: bool,
    expected_sections: list[str],
) -> None:
    """The role check runs first, and the flag can skip it.

    A replica that also evicts must be reported as a replica. The eviction
    message would point the caller at a setting on a server it should not be
    talking to.
    """

    steps: list[str] = []
    sections: list[str] = []

    class EvictingReplicaClient:
        def ping(self) -> bool:
            return True

        def info(self, section: str) -> dict[str, Any]:
            sections.append(section)
            if section == "replication":
                return {"role": "slave"}
            return {"maxmemory": 1073741824, "maxmemory_policy": "allkeys-lru"}

        def close(self) -> None:
            return None

    class FakeRedis:
        @staticmethod
        def from_url(*args: object, **kwargs: object) -> EvictingReplicaClient:
            return EvictingReplicaClient()

    class FakeModule(_FakeClientModule):
        Redis = FakeRedis

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: (FakeModule, (), _FakeWatchError, _FakeNoScriptError),
    )
    monkeypatch.setattr(RedisBackend, "_initialize_schema", lambda self: steps.append("schema"))
    monkeypatch.setattr(RedisBackend, "_detect_readonly_scripts", lambda self: False)

    expected_message = "lands on a replica" if verify_server_role else "configured to evict keys"
    with pytest.raises(UnsupportedOperation, match=expected_message):
        RedisBackend("redis://example", verify_server_role=verify_server_role)

    assert steps == []
    assert sections == expected_sections


def test_redis_backend_discards_client_after_unexpected_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected schema failures close the newly created client before escape."""

    closed = Event()

    class FakeClient:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            closed.set()

    class FakeRedis:
        @staticmethod
        def from_url(*args: object, **kwargs: object) -> FakeClient:
            return FakeClient()

    class FakeModule(_FakeClientModule):
        Redis = FakeRedis

    monkeypatch.setattr(
        redis_module,
        "_load_redis_client",
        lambda: (FakeModule, (), _FakeWatchError, _FakeNoScriptError),
    )
    monkeypatch.setattr(
        RedisBackend,
        "_initialize_schema",
        lambda self: (_ for _ in ()).throw(RuntimeError("unexpected schema failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected schema failure"):
        RedisBackend("redis://example")

    assert closed.is_set()


def test_redis_writes_report_a_spent_optimistic_retry_budget() -> None:
    """A write that keeps losing its optimistic conflict reports contention."""

    class ConflictingPipeline:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def watch(self, key: str) -> None:
            return None

        def get(self, key: str) -> str:
            return "1"

        def multi(self) -> None:
            return None

        def set(self, key: str, value: str) -> None:
            return None

        def execute(self) -> None:
            raise _FakeWatchError("WATCH conflict")

    class ConflictingClient:
        def get(self, key: str) -> str:
            return "1"

        def pipeline(self) -> ConflictingPipeline:
            return ConflictingPipeline()

        def close(self) -> None:
            return None

    backend = _detached_backend(ConflictingClient(), max_retry_time=0.0)
    attempts = 0

    def operation(transaction: redis_module._RedisTransaction) -> None:
        nonlocal attempts
        attempts += 1

    with pytest.raises(BackendBusy, match="optimistic attempts"):
        backend._transact(operation)

    assert attempts == 1


def test_redis_reads_report_a_spent_optimistic_retry_budget() -> None:
    """A read that never sees a stable guard value reports contention."""

    class MovingClient:
        def __init__(self) -> None:
            self.reads = 0

        def get(self, key: str) -> str:
            self.reads += 1
            return str(self.reads)

        def close(self) -> None:
            return None

    backend = _detached_backend(MovingClient(), max_retry_time=0.0)

    with pytest.raises(BackendBusy, match="optimistic attempts"):
        backend._snapshot(lambda client: None)


def test_redis_writes_do_not_retry_expected_memory_conflicts() -> None:
    """A staged conflict reaches the caller instead of consuming retries."""

    class UnusedClient:
        def get(self, key: str) -> None:
            return None

        def close(self) -> None:
            return None

    backend = _detached_backend(UnusedClient())

    def operation(transaction: redis_module._RedisTransaction) -> None:
        raise MemoryConflict("staged conflict")

    with pytest.raises(MemoryConflict, match="staged conflict"):
        backend._transact(operation)


class _LostReplyPipeline:
    """Pipeline whose `EXEC` is applied by the server but never answered."""

    def __init__(self, client: _LostReplyClient) -> None:
        self.client = client
        self.staged: list[str] = []
        self.token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def watch(self, key: str) -> None:
        return None

    def get(self, key: str) -> str | None:
        return self.client.guard

    def multi(self) -> None:
        return None

    def sadd(self, key: str, member: str) -> None:
        self.staged.append(member)

    def set(self, key: str, value: str) -> None:
        self.token = value

    def execute(self) -> None:
        self.client.applied.append(self.staged)
        self.client.guard = self.token
        if len(self.client.applied) > 1:
            return
        # The server committed, then the connection died before the reply
        # arrived. The client reports that as a watch conflict.
        self.client.guard = self.client.guard_after_first_commit(str(self.token))
        raise _FakeWatchError("connection lost during EXEC")


class _LostReplyClient:
    """Client whose first commit lands without its caller ever hearing back."""

    def __init__(self, *, competing_token: str | None = None) -> None:
        self.guard: str | None = None
        self.applied: list[list[str]] = []
        self.competing_token = competing_token

    def guard_after_first_commit(self, token: str) -> str:
        # A competing write replaces the token, which is what hides the proof
        # that our own commit landed.
        return self.competing_token or token

    def get(self, key: str) -> str | None:
        assert key == "genie:mutations"
        return self.guard

    def pipeline(self) -> _LostReplyPipeline:
        return _LostReplyPipeline(self)

    def close(self) -> None:
        return None


def test_redis_write_does_not_repeat_a_commit_whose_reply_was_lost() -> None:
    """The guard token proves the commit landed, so the retry returns it."""

    client = _LostReplyClient()
    backend = _detached_backend(client)
    runs = 0

    def operation(transaction: redis_module._RedisTransaction) -> str:
        nonlocal runs
        runs += 1
        transaction.stage(lambda pipeline: pipeline.sadd("genie:example", f"member-{runs}"))
        return f"result-{runs}"

    assert backend._transact(operation) == "result-1"
    assert runs == 1
    assert client.applied == [["member-1"]]


def test_redis_write_repeats_a_lost_commit_once_another_write_replaces_the_token() -> None:
    """The proof only holds while no other client has committed since."""

    client = _LostReplyClient(competing_token="written-by-another-client")
    backend = _detached_backend(client)
    runs = 0

    def operation(transaction: redis_module._RedisTransaction) -> str:
        nonlocal runs
        runs += 1
        transaction.stage(lambda pipeline: pipeline.sadd("genie:example", f"member-{runs}"))
        return f"result-{runs}"

    assert backend._transact(operation) == "result-2"
    assert runs == 2
    # The known limit: the same work is applied twice.
    assert client.applied == [["member-1"], ["member-2"]]


def test_redis_write_returns_a_lost_commit_found_on_the_last_attempt() -> None:
    """A spent budget still reports a commit the guard token can prove."""

    client = _LostReplyClient()
    backend = _detached_backend(client, max_retry_time=0.0)
    runs = 0

    def operation(transaction: redis_module._RedisTransaction) -> str:
        nonlocal runs
        runs += 1
        transaction.stage(lambda pipeline: pipeline.sadd("genie:example", f"member-{runs}"))
        return f"result-{runs}"

    assert backend._transact(operation) == "result-1"
    assert runs == 1
    assert client.applied == [["member-1"]]


def test_redis_write_reports_a_spent_budget_when_the_token_is_gone() -> None:
    """Without that proof a spent budget still reports contention."""

    client = _LostReplyClient(competing_token="written-by-another-client")
    backend = _detached_backend(client, max_retry_time=0.0)

    def operation(transaction: redis_module._RedisTransaction) -> str:
        transaction.stage(lambda pipeline: pipeline.sadd("genie:example", "member"))
        return "result"

    with pytest.raises(BackendBusy, match="lost 1 optimistic attempt"):
        backend._transact(operation)


def _detached_backend(
    client: Any,
    *,
    exception_types: tuple[type[BaseException], ...] = (),
    max_retry_time: float = 5.0,
    readonly_scripts: bool = False,
) -> RedisBackend:
    """Build a backend around a fake client without touching a server."""

    backend = object.__new__(RedisBackend)
    backend.url = "redis://example"
    backend.key_prefix = "genie"
    backend.connect_timeout = 1.0
    backend.socket_timeout = 1.0
    backend.max_retry_time = max_retry_time
    backend._client = client
    backend._exception_types = exception_types
    backend._watch_error = _FakeWatchError
    backend._no_script_error = _FakeNoScriptError
    backend._readonly_scripts = readonly_scripts
    backend._lock = RLock()
    backend._lifecycle = Condition(backend._lock)
    backend._active_operations = 0
    backend._closing = False
    return backend


def test_redis_contains_runs_as_one_read_only_script() -> None:
    """A supported server answers containment with a single atomic script."""

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> int:
            self.calls.append((digest, numkeys, *arguments))
            return 1

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.contains("root", "child") is True
    assert client.calls == [(redis_module._LUA_DIGESTS["contains"], 1, "genie:block:root", "genie", "root", "child")]


def _stored_payload(block: MemoryBlock, storage_order: int) -> str:
    """Return the stored document payload for one block."""

    return redis_module._dump_json({**redis_module._encode_block(block), "storage_order": storage_order}, field_name="memory block")


def _stored_temporal_payload(entry: TemporalEntry) -> str:
    """Return the stored payload for one temporal collection entry."""

    return redis_module._dump_json(redis_module._temporal_payload(entry), field_name="temporal entry")


def _stored_link_payload(link: MemoryLink, storage_order: int) -> str:
    """Return the stored document payload for one link."""

    return redis_module._dump_json({**redis_module._encode_link(link), "storage_order": storage_order}, field_name="memory link")


def _endpoint_member(link: MemoryLink) -> str:
    """Return the endpoint index member one link is stored under."""

    return redis_module._identity(link.id, link.source_id, link.target_id, link.relation)


def test_redis_ancestors_runs_as_one_read_only_script() -> None:
    """A supported server returns the whole parent chain from one script."""

    root = MemoryBlock(id="root", type="project", value={})
    middle = MemoryBlock(id="middle", parent_id=root.id, type="step", value={})
    leaf = MemoryBlock(id="leaf", parent_id=middle.id, type="step", value={})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_payload(leaf, 2), _stored_payload(middle, 1), _stored_payload(root, 0)]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    # The script returns the starting block first, and `ancestors` drops it.
    assert backend.ancestors("leaf") == [middle, root]
    assert client.calls == [(redis_module._LUA_DIGESTS["ancestors"], 1, "genie:block:leaf", "genie", "leaf")]


def test_redis_ancestors_returns_nothing_for_a_missing_block() -> None:
    """An empty scripted reply means the starting block itself is gone."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            return []

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    assert backend.ancestors("missing") == []


def test_redis_children_runs_as_one_read_only_script() -> None:
    """A supported server returns the child page from one script."""

    first = MemoryBlock(id="first", parent_id="root", type="step", value={})
    second = MemoryBlock(id="second", parent_id="root", type="step", value={})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_payload(first, 1), _stored_payload(second, 2)]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.children("root") == [first, second]
    assert client.calls == [(redis_module._LUA_DIGESTS["children"], 1, "genie:children:root", "genie", "root")]


def test_redis_children_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded two-command read."""

    child = MemoryBlock(id="child", parent_id="root", type="step", value={})

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str | None:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "3"

        def zrange(self, key: str, start: int, stop: int) -> list[str]:
            assert (key, start, stop) == ("genie:children:root", 0, -1)
            return ["child", "removed"]

        def mget(self, keys: list[str]) -> list[str | None]:
            assert keys == ["genie:block:child", "genie:block:removed"]
            return [_stored_payload(child, 1), None]

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.children("root") == [child]
    assert client.guard_reads == 2


def test_redis_list_links_runs_as_one_read_only_script() -> None:
    """A supported server returns connected links from one script."""

    outgoing = MemoryLink("root", "child", "owns", id="outgoing")
    incoming = MemoryLink("other", "root", "notes", id="incoming")

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_link_payload(outgoing, 0), _stored_link_payload(incoming, 1)]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    with pytest.raises(ValueError, match="direction must be one of"):
        backend.list_links("root", direction=cast(Literal["out", "in", "both"], "sideways"))
    assert client.calls == []
    assert backend.list_links("root", direction="both") == [outgoing, incoming]
    assert client.calls == [
        (redis_module._LUA_DIGESTS["list_links"], 2, "genie:outlinks:root", "genie:inlinks:root", "genie", "root", "both", "0", ""),
    ]


def test_redis_list_links_filters_the_relation_inside_the_script() -> None:
    """The relation reaches the script, so it reads only matching documents."""

    incoming = MemoryLink("other", "root", "notes", id="incoming")

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_link_payload(incoming, 1)]

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.list_links("root", relation="notes", direction="both") == [incoming]
    assert client.calls == [
        (redis_module._LUA_DIGESTS["list_links"], 2, "genie:outlinks:root", "genie:inlinks:root", "genie", "root", "both", "1", "notes"),
    ]


def test_redis_list_links_treats_an_empty_relation_as_a_filter() -> None:
    """An empty string is a valid relation, so the filter flag stays set."""

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return []

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.list_links("root", relation="") == []
    assert client.calls == [
        (redis_module._LUA_DIGESTS["list_links"], 2, "genie:outlinks:root", "genie:inlinks:root", "genie", "root", "out", "1", ""),
    ]


def test_redis_list_links_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded two-command read."""

    link = MemoryLink("root", "child", "owns", id="link")
    removed = MemoryLink("root", "gone", "owns", id="removed")

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "5"

        def zrange(self, key: str, start: int, stop: int) -> list[str]:
            assert (key, start, stop) == ("genie:outlinks:root", 0, -1)
            return [_endpoint_member(link), _endpoint_member(removed)]

        def mget(self, keys: list[str]) -> list[str | None]:
            assert keys == ["genie:link:link", "genie:link:removed"]
            return [_stored_link_payload(link, 0), None]

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.list_links("root") == [link]
    assert client.guard_reads == 2


def test_redis_link_cleanup_reads_only_the_endpoint_index() -> None:
    """Removing links uses the endpoint members, so no link document is read.

    A link document that is already gone would otherwise leave its index
    entries behind, because a missing document is skipped instead of reported.
    """

    kept = MemoryLink("root", "child", "cites", id="kept")
    shared = MemoryLink("other", "root", "cites", id="shared")

    class RecordingPipeline:
        def __init__(self, client: RecordingClient) -> None:
            self.client = client
            self.replies: list[Any] = []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def watch(self, key: str) -> None:
            return None

        def multi(self) -> None:
            return None

        def get(self, key: str) -> str:
            return "1"

        def zrange(self, key: str, start: int, stop: int) -> None:
            self.replies.append(self.client.index.get(key, []))

        def mget(self, keys: list[str]) -> None:
            raise AssertionError("link cleanup must not read link documents")

        def delete(self, *keys: str) -> None:
            self.client.commands.append(("delete", *keys))

        def zrem(self, key: str, *members: str) -> None:
            self.client.commands.append(("zrem", key, *members))

        def set(self, key: str, value: str) -> None:
            self.client.commands.append(("set", key))

        def execute(self) -> list[Any]:
            replies, self.replies = self.replies, []
            return replies

    class RecordingClient:
        def __init__(self, index: dict[str, list[str]]) -> None:
            self.index = index
            self.commands: list[tuple[str, ...]] = []

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            return "1"

        def mget(self, keys: list[str]) -> None:
            raise AssertionError("link cleanup must not read link documents")

        def pipeline(self) -> RecordingPipeline:
            return RecordingPipeline(self)

        def close(self) -> None:
            return None

    client = RecordingClient(
        {
            "genie:outlinks:root": [_endpoint_member(kept)],
            "genie:inlinks:root": [_endpoint_member(shared)],
        }
    )
    backend = _detached_backend(client)

    backend.delete_links_for_block("root")

    # Both links share the `cites` relation, so that index loses exactly two
    # members instead of being dropped whole.
    assert client.commands == [
        ("delete", "genie:outlinks:root", "genie:inlinks:root"),
        ("delete", "genie:link:kept", "genie:link:shared"),
        ("zrem", "genie:inlinks:child", _endpoint_member(kept)),
        ("zrem", "genie:relation:cites", redis_module._identity("kept", "root", "child"), redis_module._identity("shared", "other", "root")),
        ("zrem", "genie:outlinks:other", _endpoint_member(shared)),
        ("set", "genie:mutations"),
    ]


def test_redis_list_links_filters_the_relation_before_reading_documents() -> None:
    """Without scripting the filter runs on the index, so one document is read."""

    kept = MemoryLink("root", "child", "notes", id="kept")
    skipped = MemoryLink("root", "other", "owns", id="skipped")

    class GuardedClient:
        def __init__(self) -> None:
            self.reads: list[list[str]] = []

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            return "7"

        def zrange(self, key: str, start: int, stop: int) -> list[str]:
            assert (key, start, stop) == ("genie:outlinks:root", 0, -1)
            return [_endpoint_member(kept), _endpoint_member(skipped)]

        def mget(self, keys: list[str]) -> list[str | None]:
            self.reads.append(keys)
            return [_stored_link_payload(kept, 0)]

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.list_links("root", relation="notes") == [kept]
    assert client.reads == [["genie:link:kept"]]


def test_redis_temporal_page_runs_as_one_read_only_script() -> None:
    """A supported server returns one entry page from one script."""

    first = TemporalEntry("collection", "first", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = TemporalEntry("collection", "second", occurred_at=datetime(2026, 2, 1, tzinfo=UTC))

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[Any]:
            self.calls.append((digest, numkeys, *arguments))
            return [2, [first.block_id, _stored_temporal_payload(first), second.block_id, _stored_temporal_payload(second)]]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.list_temporal_entries("collection", after_block_id="earlier", limit=20) == [first, second]
    assert client.calls == [
        (
            redis_module._LUA_DIGESTS["list_temporal_entries"],
            2,
            "genie:temporalindex:collection",
            "genie:temporal:collection",
            "genie",
            "collection",
            "(earlier",
            20,
        ),
    ]


def test_redis_temporal_page_script_is_skipped_for_an_empty_page() -> None:
    """A zero limit answers without any call, on either read path."""

    class UnusedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            raise AssertionError("a zero limit must not reach the server")

        def close(self) -> None:
            return None

    backend = _detached_backend(UnusedClient(), readonly_scripts=True)

    assert backend.list_temporal_entries("collection", limit=0) == []
    with pytest.raises(ValueError, match="limit must be greater than or equal to zero"):
        backend.list_temporal_entries("collection", limit=-1)


def test_redis_temporal_page_rejects_an_unpaired_script_reply() -> None:
    """A reply that lost half of a pair fails as corruption, not an index error."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[Any]:
            return [1, ["first"]]

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    with pytest.raises(BackendCorruption, match="unpaired reply"):
        backend.list_temporal_entries("collection")


def test_redis_temporal_page_rejects_a_reply_without_a_scanned_count() -> None:
    """A page reply must carry the scanned count beside its pairs."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[Any]:
            return [["first", "{}"]]

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    with pytest.raises(BackendCorruption, match="scanned count and a page"):
        backend.list_temporal_entries("collection")


def test_redis_temporal_query_answers_one_page_through_the_script() -> None:
    """A collection that fits in one page is filtered from one atomic read."""

    kept = TemporalEntry("collection", "kept", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    later = TemporalEntry("collection", "later", occurred_at=datetime(2026, 6, 1, tzinfo=UTC))

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[Any]:
            self.calls.append((digest, numkeys, *arguments))
            return [2, [kept.block_id, _stored_temporal_payload(kept), later.block_id, _stored_temporal_payload(later)]]

        def get(self, key: str) -> None:
            raise AssertionError("a query answered inside one page must not read the mutation guard")

        def zrangebylex(self, *arguments: Any, **keywords: Any) -> list[str]:
            raise AssertionError("a query answered inside one page must not fall back to the guarded scan")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.query_temporal_entries("collection", occurred_stop=datetime(2026, 3, 1, tzinfo=UTC)) == [kept]
    assert client.calls == [
        (
            redis_module._LUA_DIGESTS["list_temporal_entries"],
            2,
            "genie:temporalindex:collection",
            "genie:temporal:collection",
            "genie",
            "collection",
            "-",
            redis_module._TEMPORAL_SCAN_PAGE,
        ),
    ]


def test_redis_temporal_query_falls_back_when_one_page_is_not_the_whole_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full first page cannot end the query, so the guarded scan takes over."""

    monkeypatch.setattr(redis_module, "_TEMPORAL_SCAN_PAGE", 2)
    early = TemporalEntry("collection", "a", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    late = TemporalEntry("collection", "b", occurred_at=datetime(2026, 6, 1, tzinfo=UTC))
    middle = TemporalEntry("collection", "c", occurred_at=datetime(2026, 2, 1, tzinfo=UTC))
    stored = {entry.block_id: _stored_temporal_payload(entry) for entry in (early, late, middle)}

    class ScriptedClient:
        def __init__(self) -> None:
            self.scripts = 0
            self.guard_reads = 0
            self.pages: list[str] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[Any]:
            self.scripts += 1
            return [2, [early.block_id, stored[early.block_id], late.block_id, stored[late.block_id]]]

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "3"

        def zrangebylex(self, key: str, lowest: str, highest: str, start: int, num: int) -> list[str]:
            assert (key, highest, start, num) == ("genie:temporalindex:collection", "+", 0, 2)
            self.pages.append(lowest)
            return ["a", "b"] if lowest == "-" else ["c"]

        def hmget(self, key: str, fields: list[str]) -> list[str]:
            assert key == "genie:temporal:collection"
            return [stored[field] for field in fields]

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.query_temporal_entries("collection", occurred_stop=datetime(2026, 3, 1, tzinfo=UTC)) == [early, middle]
    assert (client.scripts, client.pages, client.guard_reads) == (1, ["-", "(b"], 2)


def test_redis_temporal_page_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded two-command page read."""

    entry = TemporalEntry("collection", "kept", occurred_at=datetime(2026, 3, 1, tzinfo=UTC))

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "9"

        def zrangebylex(self, key: str, lowest: str, highest: str, start: int, num: int) -> list[str]:
            assert (key, lowest, highest, start, num) == ("genie:temporalindex:collection", "-", "+", 0, 100)
            return ["kept", "removed"]

        def hmget(self, key: str, fields: list[str]) -> list[str | None]:
            assert (key, fields) == ("genie:temporal:collection", ["kept", "removed"])
            return [_stored_temporal_payload(entry), None]

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.list_temporal_entries("collection") == [entry]
    assert client.guard_reads == 2


def test_redis_descendants_orders_one_scripted_subtree_reply() -> None:
    """Edges from the script become depth-first creation order in Python."""

    root = MemoryBlock(id="root", type="project", value={})
    first = MemoryBlock(id="first", parent_id="root", type="step", value={})
    second = MemoryBlock(id="second", parent_id="root", type="step", value={})
    nested = MemoryBlock(id="nested", parent_id="first", type="step", value={})
    orphaned = MemoryBlock(id="orphaned", parent_id="gone", type="step", value={})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[list[str]]:
            self.calls.append((digest, numkeys, *arguments))
            # "gone" is listed as a child of "root" but its document is missing,
            # so the walk keeps following it and only drops it from the result.
            edges = ["root", "first", "root", "second", "root", "gone", "first", "nested", "gone", "orphaned"]
            documents = [
                "root",
                _stored_payload(root, 0),
                "first",
                _stored_payload(first, 1),
                "second",
                _stored_payload(second, 2),
                "nested",
                _stored_payload(nested, 3),
                "orphaned",
                _stored_payload(orphaned, 4),
            ]
            return [edges, documents]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [block.id for block in backend.descendants("root", include_self=True)] == ["root", "first", "nested", "second", "orphaned"]
    assert [block.id for block in backend.descendants("root")] == ["first", "nested", "second", "orphaned"]
    assert client.calls[0] == (redis_module._LUA_DIGESTS["descendants"], 2, "genie:block:root", "genie:children:root", "genie", "root")


def test_redis_descendants_returns_nothing_for_a_missing_root() -> None:
    """An empty scripted reply answers with no blocks, as the guarded read does."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[list[str]]:
            return [[], []]

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    assert backend.descendants("missing", include_self=True) == []


@pytest.mark.parametrize(
    "reply",
    [
        [[]],
        [["root"], []],
        [[], ["root"]],
    ],
)
def test_redis_descendants_rejects_a_malformed_script_reply(reply: list[list[str]]) -> None:
    """A reply that is not two paired lists fails as corruption."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[list[str]]:
            return reply

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    with pytest.raises(BackendCorruption, match="malformed reply"):
        backend.descendants("root")


def test_redis_ancestors_reports_a_corrupt_document_from_a_script() -> None:
    """A malformed payload fails through the corruption boundary as before."""

    class ScriptedClient:
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            return ['{"id":"root","created_at":1}']

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    with pytest.raises(BackendCorruption, match="memory block is invalid"):
        backend.ancestors("leaf")


def test_redis_ancestors_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded parent-chain walk."""

    root = MemoryBlock(id="root", type="project", value={})
    leaf = MemoryBlock(id="leaf", parent_id=root.id, type="step", value={})
    documents = {"genie:block:root": _stored_payload(root, 0), "genie:block:leaf": _stored_payload(leaf, 1)}

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str | None:
            if key == "genie:mutations":
                self.guard_reads += 1
                return "7"
            return documents.get(key)

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.ancestors("leaf") == [root]
    assert client.guard_reads == 2


def test_redis_self_scope_search_reads_one_key_without_a_guard_or_a_script() -> None:
    """A `Scope.SELF` search is a single GET, so it can never report contention."""

    block = MemoryBlock(id="child", type="step", value={"text": "alpha"})

    class Client:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def get(self, key: str) -> str | None:
            assert key != "genie:mutations", "a single-key read must not read the mutation guard"
            self.keys.append(key)
            return _stored_payload(block, 1) if key == "genie:block:child" else None

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            raise AssertionError("a single-key read must not run a script")

        def close(self) -> None:
            return None

    client = Client()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [result.block for result in backend.search(MemoryQuery(root_id="child", scope=Scope.SELF))] == [block]
    assert backend.search(MemoryQuery(root_id="missing", scope=Scope.SELF)) == []
    assert client.keys == ["genie:block:child", "genie:block:missing"]


def test_redis_subtree_scope_search_runs_as_one_read_only_script() -> None:
    """A `Scope.SUBTREE` search reuses the scripted subtree read."""

    root = MemoryBlock(id="root", type="project", value={"text": "alpha"})
    child = MemoryBlock(id="child", parent_id="root", type="step", value={"text": "alpha"})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[list[str]]:
            self.calls.append((digest, numkeys, *arguments))
            return [["root", "child"], ["root", _stored_payload(root, 0), "child", _stored_payload(child, 1)]]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [result.block.id for result in backend.search(MemoryQuery(root_id="root", scope=Scope.SUBTREE))] == ["root", "child"]
    assert client.calls == [(redis_module._LUA_DIGESTS["descendants"], 2, "genie:block:root", "genie:children:root", "genie", "root")]


def test_redis_ancestors_scope_search_keeps_the_starting_block() -> None:
    """A `Scope.ANCESTORS` search keeps the block the shared script returns first."""

    root = MemoryBlock(id="root", type="project", value={"text": "alpha"})
    leaf = MemoryBlock(id="leaf", parent_id="root", type="step", value={"text": "alpha"})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_payload(leaf, 1), _stored_payload(root, 0)]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [result.block.id for result in backend.search(MemoryQuery(root_id="leaf", scope=Scope.ANCESTORS))] == ["leaf", "root"]
    assert client.calls == [(redis_module._LUA_DIGESTS["ancestors"], 1, "genie:block:leaf", "genie", "leaf")]


def test_redis_ancestors_scope_search_falls_back_to_the_guard() -> None:
    """Without scripting the scoped search keeps the guarded parent-chain walk."""

    root = MemoryBlock(id="root", type="project", value={"text": "alpha"})
    leaf = MemoryBlock(id="leaf", parent_id="root", type="step", value={"text": "alpha"})
    documents = {"genie:block:root": _stored_payload(root, 0), "genie:block:leaf": _stored_payload(leaf, 1)}

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str | None:
            if key == "genie:mutations":
                self.guard_reads += 1
                return "9"
            return documents.get(key)

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert [result.block.id for result in backend.search(MemoryQuery(root_id="leaf", scope=Scope.ANCESTORS))] == ["leaf", "root"]
    assert backend.search(MemoryQuery(root_id="missing", scope=Scope.ANCESTORS)) == []
    assert client.guard_reads == 4


def test_redis_global_scope_search_keeps_the_mutation_guard() -> None:
    """A global search reads every stored block, so a script must not hold the server."""

    first = MemoryBlock(id="first", type="item", value={"text": "alpha"})
    second = MemoryBlock(id="second", type="item", value={"text": "alpha"})
    documents = {"genie:block:first": _stored_payload(first, 0), "genie:block:second": _stored_payload(second, 1)}

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "2"

        def zrange(self, key: str, start: int, stop: int) -> list[str]:
            assert (key, start, stop) == ("genie:blocks", 0, -1)
            return ["first", "second"]

        def mget(self, keys: list[str]) -> list[str | None]:
            return [documents.get(key) for key in keys]

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            raise AssertionError("an unbounded read must not run a script")

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [result.block.id for result in backend.search(MemoryQuery())] == ["first", "second"]
    assert client.guard_reads == 2


def test_redis_links_scope_search_keeps_the_mutation_guard() -> None:
    """One block can carry any number of links, so this scope stays guarded."""

    first = MemoryBlock(id="first", type="item", value={"text": "alpha"})
    second = MemoryBlock(id="second", type="item", value={"text": "alpha"})
    link = MemoryLink("first", "second", "relates_to", id="link")
    documents = {"genie:block:first": _stored_payload(first, 0), "genie:block:second": _stored_payload(second, 1)}

    class EndpointPipeline:
        def __init__(self) -> None:
            self.replies: list[list[tuple[str, float]]] = []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> None:
            assert withscores is True
            self.replies.append([(_endpoint_member(link), 0.0)] if key == "genie:outlinks:first" else [])

        def execute(self) -> list[list[tuple[str, float]]]:
            return self.replies

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str | None:
            if key == "genie:mutations":
                self.guard_reads += 1
                return "5"
            return documents.get(key)

        def mget(self, keys: list[str]) -> list[str | None]:
            return [documents.get(key) for key in keys]

        def pipeline(self) -> EndpointPipeline:
            return EndpointPipeline()

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            raise AssertionError("an unbounded read must not run a script")

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert [result.block.id for result in backend.search(MemoryQuery(root_id="first", scope=Scope.LINKS))] == ["first", "second"]
    assert backend.search(MemoryQuery(root_id="missing", scope=Scope.LINKS)) == []
    assert client.guard_reads == 4


def test_redis_contains_reloads_a_script_the_server_dropped() -> None:
    """A flushed script cache is refilled by sending the body once."""

    class ScriptedClient:
        def __init__(self) -> None:
            self.bodies: list[str] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> int:
            raise _FakeNoScriptError("NOSCRIPT No matching script")

        def eval_ro(self, body: str, numkeys: int, *arguments: str) -> int:
            self.bodies.append(body)
            return 0

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, exception_types=(_FakeNoScriptError,), readonly_scripts=True)

    assert backend.contains("root", "missing") is False
    assert client.bodies == [redis_module._LUA_SCRIPTS["contains"]]


def test_redis_contains_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded multi-command read."""

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> None:
            self.guard_reads += 1

        def exists(self, key: str) -> int:
            return 0

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.contains("root", "child") is False
    assert client.guard_reads == 2


def test_redis_read_only_script_probe_detects_server_support() -> None:
    """The probe runs a trivial script instead of reading a version string."""

    class ScriptingClient:
        def eval_ro(self, body: str, numkeys: int, *arguments: str) -> int:
            assert (body, numkeys) == ("return 1", 0)
            return 1

        def close(self) -> None:
            return None

    assert _detached_backend(ScriptingClient())._detect_readonly_scripts() is True


def test_redis_read_only_script_probe_accepts_servers_without_support() -> None:
    """A refused command turns the scripted reads off without failing."""

    class ResponseError(Exception):
        """Stand-in for the client's command rejection error."""

    class OldServerClient:
        def eval_ro(self, body: str, numkeys: int, *arguments: str) -> int:
            raise ResponseError("unknown command 'EVAL_RO'")

        def close(self) -> None:
            return None

    backend = _detached_backend(OldServerClient(), exception_types=(ResponseError,))

    assert backend._detect_readonly_scripts() is False


@pytest.mark.parametrize(
    ("report", "is_replica"),
    [
        pytest.param({"role": "slave"}, True, id="replica_role"),
        pytest.param({"role": " REPLICA "}, True, id="padded_upper_case_replica_word"),
        pytest.param({"role": "master"}, False, id="primary_role"),
        pytest.param({"role": "sentinel"}, False, id="unrecognized_role"),
        pytest.param({"role": "slave_read_only"}, False, id="role_that_only_starts_with_a_replica_word"),
        pytest.param({}, False, id="missing_role"),
        pytest.param({"role": 8}, False, id="unexpected_role_type"),
        pytest.param("not a mapping", False, id="unexpected_reply"),
    ],
)
def test_redis_role_probe_refuses_only_a_server_it_can_prove_is_a_replica(
    report: Any,
    is_replica: bool,
) -> None:
    """A proven replica role fails; every unknown reading is accepted."""

    class ReportingClient:
        def info(self, section: str) -> Any:
            assert section == "replication"
            return report

        def close(self) -> None:
            return None

    backend = _detached_backend(ReportingClient())

    if not is_replica:
        assert backend._verify_server_role() is None
        return
    with pytest.raises(UnsupportedOperation, match="lands on a replica"):
        backend._verify_server_role()


def test_redis_role_probe_accepts_a_server_that_blocks_the_info_command() -> None:
    """A refused `INFO` leaves the role unknown instead of failing."""

    class ResponseError(Exception):
        """Stand-in for the client's command rejection error."""

    class RestrictedClient:
        def info(self, section: str) -> dict[str, Any]:
            raise ResponseError("NOPERM this user has no permissions to run the 'info' command")

        def close(self) -> None:
            return None

    backend = _detached_backend(RestrictedClient(), exception_types=(ResponseError,))

    assert backend._verify_server_role() is None


def test_redis_role_probe_accepts_clients_without_support() -> None:
    """A client library with no `info` leaves the role unknown."""

    class MinimalClient:
        def close(self) -> None:
            return None

    assert _detached_backend(MinimalClient())._verify_server_role() is None


def test_redis_role_probe_reports_an_unreachable_server() -> None:
    """A transport failure reaches the caller instead of passing the check."""

    class ConnectionError(Exception):
        """Stand-in for the client's transport failure."""

    class UnreachableClient:
        def info(self, section: str) -> dict[str, Any]:
            raise ConnectionError("Connection closed by server")

        def close(self) -> None:
            return None

    backend = _detached_backend(UnreachableClient(), exception_types=(ConnectionError,))

    with pytest.raises(ConnectionError):
        backend._verify_server_role()


@pytest.mark.parametrize(
    ("report", "evicts"),
    [
        pytest.param({"maxmemory": 1073741824, "maxmemory_policy": "allkeys-lru"}, True, id="limit_with_allkeys_policy"),
        pytest.param({"maxmemory": "1073741824", "maxmemory_policy": "ALLKEYS-LFU"}, True, id="string_limit_and_upper_case_policy"),
        pytest.param({"maxmemory": 1073741824, "maxmemory_policy": "noeviction"}, False, id="limit_without_eviction"),
        pytest.param({"maxmemory": 1073741824, "maxmemory_policy": "volatile-lru"}, False, id="expiry_only_eviction"),
        pytest.param({"maxmemory": 0, "maxmemory_policy": "allkeys-lru"}, False, id="eviction_policy_without_a_limit"),
        pytest.param({"maxmemory_policy": "allkeys-lru"}, False, id="missing_limit"),
        pytest.param({"maxmemory": "unknown", "maxmemory_policy": "allkeys-lru"}, False, id="unreadable_limit"),
        pytest.param({"maxmemory": True, "maxmemory_policy": "allkeys-lru"}, False, id="unexpected_limit_type"),
        pytest.param({"maxmemory": 1073741824}, False, id="missing_policy"),
        pytest.param({"maxmemory": 1073741824, "maxmemory_policy": 8}, False, id="unexpected_policy_type"),
        pytest.param("not a mapping", False, id="unexpected_reply"),
    ],
)
def test_redis_eviction_probe_refuses_only_a_server_it_can_prove_evicts(
    report: Any,
    evicts: bool,
) -> None:
    """A proven eviction setting fails; every unknown reading is accepted."""

    class ReportingClient:
        def info(self, section: str) -> Any:
            assert section == "memory"
            return report

        def close(self) -> None:
            return None

    backend = _detached_backend(ReportingClient())

    if not evicts:
        assert backend._verify_eviction_policy() is None
        return
    with pytest.raises(UnsupportedOperation, match="configured to evict keys"):
        backend._verify_eviction_policy()


def test_redis_eviction_probe_accepts_a_server_that_blocks_the_info_command() -> None:
    """A refused `INFO` leaves the setting unknown instead of failing."""

    class ResponseError(Exception):
        """Stand-in for the client's command rejection error."""

    class RestrictedClient:
        def info(self, section: str) -> dict[str, Any]:
            raise ResponseError("NOPERM this user has no permissions to run the 'info' command")

        def close(self) -> None:
            return None

    backend = _detached_backend(RestrictedClient(), exception_types=(ResponseError,))

    assert backend._verify_eviction_policy() is None


def test_redis_eviction_probe_accepts_clients_without_support() -> None:
    """A client library with no `info` leaves the setting unknown."""

    class MinimalClient:
        def close(self) -> None:
            return None

    assert _detached_backend(MinimalClient())._verify_eviction_policy() is None


def test_redis_eviction_probe_reports_an_unreachable_server() -> None:
    """A transport failure reaches the caller instead of passing the check."""

    class ConnectionError(Exception):
        """Stand-in for the client's transport failure."""

    class UnreachableClient:
        def info(self, section: str) -> dict[str, Any]:
            raise ConnectionError("Connection closed by server")

        def close(self) -> None:
            return None

    backend = _detached_backend(UnreachableClient(), exception_types=(ConnectionError,))

    with pytest.raises(ConnectionError):
        backend._verify_eviction_policy()


def test_redis_read_only_script_probe_accepts_clients_without_support() -> None:
    """A client library with no `eval_ro` turns the scripted reads off."""

    class OldLibraryClient:
        def close(self) -> None:
            return None

    assert _detached_backend(OldLibraryClient())._detect_readonly_scripts() is False


def test_redis_read_only_script_probe_reports_transport_failures() -> None:
    """A connection failure during the probe still reaches the caller."""

    class TransportFailure(Exception):
        pass

    class BrokenClient:
        def eval_ro(self, body: str, numkeys: int, *arguments: str) -> int:
            raise TransportFailure("Error 61 connecting to localhost:6379")

        def close(self) -> None:
            return None

    backend = _detached_backend(BrokenClient(), exception_types=(TransportFailure,))

    with pytest.raises(TransportFailure):
        backend._detect_readonly_scripts()


_LIVE_URL = os.environ.get("GENIE_REDIS_URL")
_LIVE_PREFIX = os.environ.get("GENIE_REDIS_KEY_PREFIX", "genie-test")
_LIVE_REPLICA_URL = os.environ.get("GENIE_REDIS_REPLICA_URL")
_LIVE_CLIENT_AVAILABLE = importlib.util.find_spec("redis") is not None
_LIVE_REASON = "set GENIE_REDIS_URL and install the redis extra to run live Redis tests"
_LIVE_REPLICA_REASON = "set GENIE_REDIS_URL and GENIE_REDIS_REPLICA_URL and install the redis extra to run live Redis replica tests"
_LIVE_REPLICATION_TIMEOUT = 15.0
_CLIENT_CONTRACT_REASON = "install the redis extra to check the client retry contract"


@pytest.mark.skipif(not _LIVE_CLIENT_AVAILABLE, reason=_CLIENT_CONTRACT_REASON)
def test_redis_transport_options_retry_a_dropped_connection() -> None:
    """The retry options reach the installed client and take effect.

    The client retries nothing by default, so this checks that the options the
    backend passes replace that default. It needs the client installed but no
    running server.
    """

    import redis
    import redis.connection
    import redis.exceptions

    assert redis_module._transport_options(redis, 0) == {}

    client = redis.Redis(**redis_module._transport_options(redis, 2))
    # The pool declares the narrow `ConnectionInterface`, which carries no
    # retry attributes. The pool builds a `Connection` for a `redis://` URL,
    # and that is the class holding the settings this test reads.
    connection = cast(redis.connection.Connection, client.connection_pool.make_connection())
    attempts: list[int] = []

    def dropped() -> None:
        attempts.append(1)
        raise redis.exceptions.ConnectionError("Connection closed by server.")

    assert connection.retry_on_error == [redis.exceptions.ConnectionError]
    with pytest.raises(redis.exceptions.ConnectionError):
        connection.retry.call_with_retry(dropped, lambda error: None)
    assert len(attempts) == 3

    client.close()


@pytest.mark.skipif(not _LIVE_CLIENT_AVAILABLE, reason=_CLIENT_CONTRACT_REASON)
def test_redis_client_does_not_retry_a_command_that_timed_out() -> None:
    """A command that timed out is sent once, because it may still be running.

    The retry policy catches a timeout as well, so the gate that keeps it to
    one attempt is the client's own: it re-raises every error that
    `retry_on_error` does not list, and `_transport_options` lists a dropped
    connection only. This test fails if the client ever widens that gate.
    """

    import redis
    import redis.exceptions

    client = redis.Redis(**redis_module._transport_options(redis, 2))
    connection = client.connection_pool.make_connection()

    with pytest.raises(redis.exceptions.TimeoutError):
        client._disconnect_raise(connection, redis.exceptions.TimeoutError("Timeout reading from socket."))
    assert client._disconnect_raise(connection, redis.exceptions.ConnectionError("Connection closed by server.")) is None

    client.close()


@pytest.mark.skipif(not _LIVE_CLIENT_AVAILABLE, reason=_CLIENT_CONTRACT_REASON)
def test_redis_client_retries_opening_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening a connection is retried, which multiplies `connect_timeout`.

    A server that answers nothing holds each try for the connect timeout, so
    the documented worst case is that timeout times `transport_retries` plus
    one. A server that refuses the connection is not in this case: it answers
    with an `OSError` the retry policy does not cover, so it is tried once.
    """

    import redis
    import redis.exceptions

    client = redis.Redis(**redis_module._transport_options(redis, 2))
    silent = client.connection_pool.make_connection()
    refusing = client.connection_pool.make_connection()
    silent_attempts: list[int] = []
    refusing_attempts: list[int] = []

    def times_out() -> None:
        silent_attempts.append(1)
        raise TimeoutError("timed out")

    def refuses() -> None:
        refusing_attempts.append(1)
        raise OSError(61, "Connection refused")

    # Replace the one step that opens the socket, so the retry around it is
    # what the attempt counts measure.
    monkeypatch.setattr(silent, "_connect", times_out)
    monkeypatch.setattr(refusing, "_connect", refuses)

    with pytest.raises(redis.exceptions.TimeoutError):
        silent.connect()
    with pytest.raises(redis.exceptions.ConnectionError):
        refusing.connect()

    assert len(silent_attempts) == 3
    assert len(refusing_attempts) == 1

    client.close()


@pytest.mark.skipif(not _LIVE_CLIENT_AVAILABLE, reason=_CLIENT_CONTRACT_REASON)
def test_redis_client_turns_a_dropped_watched_connection_into_a_conflict() -> None:
    """Transport retry cannot repeat a write, because an open watch blocks it.

    Every Genie write runs inside `WATCH`/`MULTI`/`EXEC`. Transport retry is
    safe only because the client refuses to send those commands again while a
    watch is open, and raises a watch conflict instead. `RedisBackend._transact`
    answers that conflict with a fresh attempt, so the write is never applied
    twice. This test fails if the client ever drops that rule.
    """

    import redis
    import redis.exceptions

    client = redis.Redis(**redis_module._transport_options(redis, 2))
    disconnected: list[str] = []

    class WatchingConnection:
        def __init__(self) -> None:
            self.retry_on_error = [redis.exceptions.ConnectionError]

        def disconnect(self) -> None:
            disconnected.append("closed")

    with client.pipeline() as pipeline:
        pipeline.watching = True
        with pytest.raises(redis.exceptions.WatchError):
            pipeline._disconnect_raise_reset(
                cast(Any, WatchingConnection()),
                redis.exceptions.ConnectionError("Connection closed by server."),
            )

    assert disconnected == ["closed"]

    client.close()


def _live_auth() -> tuple[str, str] | None:
    user = os.environ.get("GENIE_REDIS_USER")
    password = os.environ.get("GENIE_REDIS_PASSWORD")
    if user is None and password is None:
        return None
    if user is None or password is None:
        raise RuntimeError("GENIE_REDIS_USER and GENIE_REDIS_PASSWORD must be set together.")
    return user, password


def _live_backend(*, initialize_schema: bool = True, key_prefix: str | None = None) -> RedisBackend:
    assert _LIVE_URL is not None
    return RedisBackend(
        _LIVE_URL,
        auth=_live_auth(),
        key_prefix=key_prefix or _LIVE_PREFIX,
        initialize_schema=initialize_schema,
    )


def _live_replica_backend(**options: Any) -> RedisBackend:
    """Open a backend against the live replica with the given options."""

    assert _LIVE_REPLICA_URL is not None
    return RedisBackend(
        _LIVE_REPLICA_URL,
        auth=_live_auth(),
        key_prefix=_LIVE_PREFIX,
        **options,
    )


def _wait_for_replicated_key(key: str, *, present: bool = True) -> None:
    """Wait until one change made on the primary reaches the replica.

    The wait uses a plain client instead of a backend, because a backend
    opened against the replica validates the schema record during
    construction, and that record is itself something the primary has to
    replicate first.

    Args:
        key: Full Redis key to watch.
        present: Whether to wait for the key to arrive or to disappear.

    Raises:
        AssertionError: If the change does not arrive before the timeout.
    """

    import redis

    assert _LIVE_REPLICA_URL is not None
    options: dict[str, Any] = {"decode_responses": True, "socket_connect_timeout": 10.0, "socket_timeout": 10.0}
    auth = _live_auth()
    if auth is not None:
        options["username"], options["password"] = auth
    client = redis.Redis.from_url(_LIVE_REPLICA_URL, **options)
    try:
        deadline = monotonic() + _LIVE_REPLICATION_TIMEOUT
        while bool(client.exists(key)) is not present:
            if monotonic() >= deadline:
                state = "reach" if present else "leave"
                raise AssertionError(f"Key {key!r} did not {state} the replica within {_LIVE_REPLICATION_TIMEOUT} seconds.")
            sleep(0.05)
    finally:
        client.close()


def _clear_live_data(backend: RedisBackend, *, include_schema: bool = False) -> None:
    schema_key = f"{backend.key_prefix}:schema"

    def operation(client: Any) -> None:
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor, match=f"{backend.key_prefix}:*", count=500)
            removable = [key for key in keys if include_schema or key != schema_key]
            if removable:
                client.delete(*removable)
            if cursor == 0:
                return

    backend._direct(operation)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
class TestRedisStructureBackendContracts(
    BlockStoreContract,
    TraversalIndexContract,
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
):
    """Run every reusable capability contract against a live Redis server.

    This class uses the read path the server supports. On Redis 7.0 or newer
    that is the read-only script path, so
    `TestRedisGuardedReadStructureBackendContracts` covers the other one.
    """

    def setup_method(self) -> None:
        """Open one backend and clear the test key namespace."""

        self.backend = _live_backend()
        _clear_live_data(self.backend)

    def teardown_method(self) -> None:
        """Clear the test key namespace and close the backend."""

        _clear_live_data(self.backend)
        self.backend.close()

    def make_backend(self) -> RedisBackend:
        """Return the backend shared by one contract test."""

        return self.backend


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
class TestRedisGuardedReadStructureBackendContracts(TestRedisStructureBackendContracts):
    """Run every capability contract against the guarded read path.

    - `RedisBackend` implements `children`, `ancestors`, `descendants`,
      `contains`, `list_links`, and `list_temporal_entries` twice: as one
      read-only script where the server can run one, and as several commands
      under the mutation guard otherwise. `query_temporal_entries` splits the
      same way for a collection that fits in one page.
    - Which one runs is decided once during construction, so a live Redis 7
      server always exercises the script path and would leave the guarded path
      untested here.
    - `test_live_redis_scripted_and_guarded_reads_agree` already checks that
      both paths return the same values for the same stored state. These
      contracts add what that comparison cannot: paging, empty pages, ordering,
      and dependent cleanup after a write, all against the guarded path.
    """

    def setup_method(self) -> None:
        """Open one backend and force its reads onto the guarded path."""

        super().setup_method()
        self.backend._readonly_scripts = False


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_persists_and_coordinates_instances() -> None:
    """Committed state survives reconnects and concurrent instances agree."""

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

        def add(backend: RedisBackend, link: MemoryLink) -> bool:
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


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_isolates_separate_key_prefixes() -> None:
    """Two prefixes share one Redis database without seeing each other."""

    first = _live_backend(key_prefix=f"{_LIVE_PREFIX}-a")
    second = _live_backend(key_prefix=f"{_LIVE_PREFIX}-b")
    try:
        _clear_live_data(first)
        _clear_live_data(second)
        block = first.put_block(MemoryBlock(id="shared-id", type="note", value={"owner": "first"}))

        assert first.get_block(block.id) == block
        assert second.get_block(block.id) is None
        assert second.search(MemoryQuery()) == []
    finally:
        _clear_live_data(first, include_schema=True)
        _clear_live_data(second, include_schema=True)
        first.close()
        second.close()


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_preserves_link_identity_and_rolls_back_conflicts() -> None:
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


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_rejects_invalid_link_mutations_and_directions() -> None:
    """Link identity, existence, and directional reads are enforced by Redis."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        source = backend.put_block(MemoryBlock(id="source", type="node", value={}))
        target = backend.put_block(MemoryBlock(id="target", type="node", value={}))
        link = backend.add_link(MemoryLink(source.id, target.id, "related", id="link"))
        loop = backend.add_link(MemoryLink(source.id, source.id, "related", id="loop"))

        with pytest.raises(MemoryConflict, match="does not exist"):
            backend.update_link(MemoryLink(source.id, target.id, "related", id="missing"))
        with pytest.raises(MemoryConflict, match="only metadata"):
            backend.update_link(replace(link, target_id=source.id))
        with pytest.raises(ValueError, match="direction"):
            backend.list_links(source.id, direction=cast(Literal["out", "in", "both"], "sideways"))

        assert backend.list_links(target.id, direction="in") == [link]
        assert backend.list_links(source.id, relation="unrelated") == []
        assert backend.list_links(source.id, direction="both") == [link, loop]
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_searches_every_scope_and_filters_candidates() -> None:
    """Portable search applies scope, type, and text exclusion on real records."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        root = backend.put_block(MemoryBlock(id="root", type="root", value={"text": "alpha"}))
        child = backend.put_block(MemoryBlock(id="child", parent_id=root.id, type="child", value={"text": "beta"}))
        leaf = backend.put_block(MemoryBlock(id="leaf", parent_id=child.id, type="leaf", value={"text": "gamma"}))

        assert [result.block.id for result in backend.search(MemoryQuery(root_id=child.id, scope=Scope.SELF))] == [child.id]
        assert [result.block.id for result in backend.search(MemoryQuery(root_id=leaf.id, scope=Scope.ANCESTORS))] == [leaf.id, child.id, root.id]
        assert backend.search(MemoryQuery(root_id="missing", scope=Scope.SELF)) == []
        assert backend.search(MemoryQuery(types=["missing-type"])) == []
        assert backend.search(MemoryQuery("missing text")) == []
        assert {result.block.id for result in backend.search(MemoryQuery())} == {
            root.id,
            child.id,
            leaf.id,
        }
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_depth_reads_are_atomic_under_concurrent_writes() -> None:
    """Scripted depth reads and block-scoped searches never report contention."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        if not backend._readonly_scripts:
            pytest.skip("this Redis server cannot run read-only scripts")
        root = backend.put_block(MemoryBlock(id="root", type="project", value={}))
        deepest = root
        for depth in range(8):
            deepest = backend.put_block(MemoryBlock(id=f"level-{depth}", parent_id=deepest.id, type="step", value={}))
        outside = backend.put_block(MemoryBlock(id="outside", type="project", value={}))
        outgoing = backend.add_link(MemoryLink(root.id, outside.id, "relates", id="outgoing"))
        incoming = backend.add_link(MemoryLink(outside.id, root.id, "mirrors", id="incoming"))
        timed = backend.add_temporal_entry(TemporalEntry(root.id, outside.id, occurred_at=datetime(2026, 4, 1, tzinfo=UTC)))
        stop = Event()

        def keep_writing() -> int:
            writes = 0
            while not stop.is_set():
                noise = backend.put_block(MemoryBlock(id=f"noise-{writes}", parent_id=outside.id, type="noise", value={}))
                backend.delete_block(noise.id)
                writes += 1
            return writes

        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(keep_writing)
            try:
                chain = [f"level-{depth}" for depth in reversed(range(7))]
                for _ in range(200):
                    assert backend.contains(root.id, deepest.id) is True
                    assert backend.contains(outside.id, deepest.id) is False
                    assert [block.id for block in backend.ancestors(deepest.id)] == [*chain, root.id]
                    assert backend.ancestors(outside.id) == []
                    assert [block.id for block in backend.children(root.id)] == ["level-0"]
                    assert backend.children(deepest.id) == []
                    assert backend.list_links(root.id) == [outgoing]
                    assert backend.list_links(root.id, direction="in") == [incoming]
                    assert backend.list_links(root.id, direction="both") == [outgoing, incoming]
                    assert backend.list_links(root.id, relation="mirrors", direction="both") == [incoming]
                    assert backend.list_temporal_entries(root.id) == [timed]
                    assert backend.list_temporal_entries(root.id, after_block_id=outside.id) == []
                    assert [block.id for block in backend.descendants(root.id)] == list(reversed(chain)) + [deepest.id]
                    assert [block.id for block in backend.descendants(deepest.id, include_self=True)] == [deepest.id]
                    assert [result.block.id for result in backend.search(MemoryQuery(root_id=deepest.id, scope=Scope.SELF))] == [deepest.id]
                    assert [result.block.id for result in backend.search(MemoryQuery(root_id=root.id, scope=Scope.SUBTREE))] == [root.id, *reversed(chain), deepest.id]
                    assert [result.block.id for result in backend.search(MemoryQuery(root_id=deepest.id, scope=Scope.ANCESTORS))] == [deepest.id, *chain, root.id]
            finally:
                stop.set()
            assert writer.result(timeout=30) > 0
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_scripted_and_guarded_reads_agree() -> None:
    """Both read paths return the same values for the same stored state."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        if not backend._readonly_scripts:
            pytest.skip("this Redis server cannot run read-only scripts")
        root = backend.put_block(MemoryBlock(id="root", type="project", value={}))
        first = backend.put_block(MemoryBlock(id="first", parent_id=root.id, type="step", value={}))
        backend.put_block(MemoryBlock(id="second", parent_id=root.id, type="step", value={}))
        leaf = backend.put_block(MemoryBlock(id="leaf", parent_id=first.id, type="step", value={}))
        backend.add_link(MemoryLink(root.id, leaf.id, "relates", id="outgoing"))
        backend.add_link(MemoryLink(leaf.id, root.id, "mirrors", id="incoming"))
        backend.add_temporal_entry(TemporalEntry(root.id, leaf.id, occurred_at=datetime(2026, 5, 1, tzinfo=UTC)))

        def read_everything() -> tuple[Any, ...]:
            return (
                backend.contains(root.id, leaf.id),
                backend.contains(first.id, root.id),
                backend.ancestors(leaf.id),
                backend.children(root.id),
                backend.descendants(root.id, include_self=True),
                backend.descendants(leaf.id),
                backend.list_links(root.id, direction="both"),
                backend.list_links(root.id, relation="mirrors", direction="in"),
                backend.list_temporal_entries(root.id),
                backend.query_temporal_entries(root.id, occurred_start=datetime(2026, 1, 1, tzinfo=UTC)),
                backend.search(MemoryQuery(root_id=leaf.id, scope=Scope.SELF)),
                backend.search(MemoryQuery(root_id=root.id, scope=Scope.SUBTREE)),
                backend.search(MemoryQuery(root_id=leaf.id, scope=Scope.ANCESTORS)),
            )

        scripted = read_everything()
        backend._readonly_scripts = False
        assert read_everything() == scripted
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_reports_corrupt_persisted_record_types() -> None:
    """Malformed stored values fail through the backend corruption boundary."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        container = backend.put_block(MemoryBlock(id="container", type="container", value={}))
        target = backend.put_block(MemoryBlock(id="target", type="item", value={}))
        corrupt = backend.put_block(MemoryBlock(id="corrupt", type="item", value={}))
        link = backend.add_link(MemoryLink(container.id, target.id, "related", id="link"))
        address = MemoryAddress(("items",), "target")
        backend.bind_key(KeyBinding(container.id, address, target.id))
        log_entry = backend.append_log_entry(container.id, target.id)
        backend.add_temporal_entry(TemporalEntry(container.id, target.id, occurred_at=datetime(2026, 1, 1, tzinfo=UTC)))

        def corrupt_records(client: Any) -> None:
            client.set(f"{backend.key_prefix}:block:{corrupt.id}", '{"id":"corrupt","created_at":1}')
            client.set(f"{backend.key_prefix}:link:{link.id}", '{"id":"link","created_at":1}')
            client.hset(f"{backend.key_prefix}:keys:{container.id}", "{}", target.id)
            client.zadd(f"{backend.key_prefix}:log:{container.id}", {'["one","target"]': log_entry.sequence + 1})
            client.hset(f"{backend.key_prefix}:temporal:{container.id}", target.id, '{"occurred_at":"invalid"}')

        backend._direct(corrupt_records)

        with pytest.raises(BackendCorruption, match="memory block is invalid"):
            backend.get_block(corrupt.id)
        with pytest.raises(BackendCorruption, match="memory link is invalid"):
            backend.list_links(container.id)
        with pytest.raises(BackendCorruption, match="key address is invalid"):
            backend.list_keys(container.id)
        with pytest.raises(BackendCorruption, match="log entry is invalid"):
            backend.list_log_entries(container.id)
        with pytest.raises(BackendCorruption, match="timestamp is invalid"):
            backend.get_temporal_entry(container.id, target.id)
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_matches_core_ownership_search_and_delete_behavior() -> None:
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


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_explicit_cleanup_capabilities() -> None:
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

        entry = TemporalEntry(container.id, first.id, occurred_at=first.created_at)
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


# Key kinds that always exist once the schema record is written. They belong to
# the dataset rather than to any one block or link.
_SHARED_KEY_KINDS = frozenset({"schema", "mutations", "blocks", "acyclic", "order"})

# Key kinds whose last segment is the id of the block that owns them. The two
# endpoint index kinds are owned by a block too, but they are checked on their
# own because their members must resolve as well.
_BLOCK_KEY_KINDS = frozenset(
    {
        "block",
        "children",
        "keys",
        "keyowner",
        "log",
        "logcounter",
        "logmember",
        "temporal",
        "temporalindex",
        "temporalmember",
    }
)

_ENDPOINT_KEY_KINDS = frozenset({"outlinks", "inlinks"})


def _split_key(key: str) -> tuple[str, str]:
    """Return the kind of one Genie key and the name that follows it."""

    parts = key.split(":", 2)
    return parts[1], parts[2] if len(parts) > 2 else ""


def _stored_members(backend: RedisBackend, key: str) -> list[str]:
    """Return every member of one stored sorted set."""

    return [str(member) for member in backend._direct(lambda client: client.zrange(key, 0, -1))]


def _orphan_keys(backend: RedisBackend) -> list[str]:
    """Return every stored Genie key that no live block or link still owns.

    - The check walks the keys the server actually holds and resolves each one
      back to a live owner, including the members an index holds.
    - It compares against no written list of expected keys, so a cleanup path
      that forgets a key fails here even when nobody remembers to update the
      test.
    - A key kind this helper does not know is reported too, so a new key pattern
      cannot be added without classifying it.
    """

    prefix = backend.key_prefix
    stored = sorted(str(key) for key in backend._direct(lambda client: client.keys(f"{prefix}:*")))
    parsed = [(key, *_split_key(key)) for key in stored]
    live_blocks = {str(member) for member in backend._direct(lambda client: client.zrange(f"{prefix}:blocks", 0, -1))}
    stored_links = {name for _, kind, name in parsed if kind == "link"}
    endpoints = {key: _stored_members(backend, key) for key, kind, _ in parsed if kind in _ENDPOINT_KEY_KINDS}
    linked = {
        redis_module._decode_endpoint_member(member)[0]
        for key, kind, name in parsed
        if kind in _ENDPOINT_KEY_KINDS and name in live_blocks
        for member in endpoints[key]
    }
    orphans: list[str] = []
    for key, kind, name in parsed:
        if kind in _SHARED_KEY_KINDS:
            continue
        if kind in _ENDPOINT_KEY_KINDS:
            # A live block can still point at a link whose document is gone.
            members = [redis_module._decode_endpoint_member(member)[0] for member in endpoints[key]]
            if name not in live_blocks or any(link_id not in stored_links for link_id in members):
                orphans.append(key)
        elif kind in _BLOCK_KEY_KINDS:
            if name not in live_blocks:
                orphans.append(key)
        elif kind == "link":
            if name not in linked:
                orphans.append(key)
        elif kind == "relation":
            edges = [redis_module._decode_identity(member, size=3, name="link relation edge") for member in _stored_members(backend, key)]
            if any(edge[0] not in linked for edge in edges):
                orphans.append(key)
        else:
            orphans.append(key)
    return orphans


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_leaves_no_orphan_keys_after_deletion() -> None:
    """A deleted subtree removes every index and reverse-index key it owned."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        root = backend.put_block(MemoryBlock(id="root", type="root", value={}))
        child = backend.put_block(MemoryBlock(id="child", parent_id=root.id, type="child", value={}))
        outside = backend.put_block(MemoryBlock(id="outside", type="other", value={}))
        neighbor = backend.put_block(MemoryBlock(id="neighbor", type="other", value={}))
        backend.add_link(MemoryLink(child.id, outside.id, "cites"))
        backend.add_link(MemoryLink(outside.id, child.id, "references"))
        # A link between two surviving blocks shares the `cites` relation with a
        # deleted one, so the relation index has to lose one member and keep the
        # other instead of being dropped whole.
        survivor = backend.add_link(MemoryLink(outside.id, neighbor.id, "cites"))
        backend.bind_key(KeyBinding(root.id, MemoryAddress((), "child"), child.id))
        backend.bind_key(KeyBinding(outside.id, MemoryAddress((), "child"), child.id))
        backend.append_log_entry(root.id, child.id)
        backend.append_log_entry(outside.id, child.id)
        backend.add_temporal_entry(TemporalEntry(root.id, child.id, occurred_at=child.created_at))
        backend.add_temporal_entry(TemporalEntry(outside.id, child.id, occurred_at=child.created_at))

        backend.delete_block(root.id)

        assert _orphan_keys(backend) == []
        assert backend.list_links(outside.id, direction="both") == [survivor]
        assert backend.list_links(outside.id, relation="cites") == [survivor]
        assert backend.list_links(neighbor.id, direction="in") == [survivor]
        assert backend.list_keys(outside.id) == []
        assert backend.list_log_entries(outside.id) == []
        assert backend.list_temporal_entries(outside.id) == []

        # Deleting the last endpoints must also empty the relation index.
        backend.delete_block(outside.id)
        backend.delete_block(neighbor.id)
        assert _orphan_keys(backend) == []
        _clear_live_data(backend)


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_rejects_stale_writes_across_instances() -> None:
    """The mutation guard protects CAS and subtree snapshots across clients."""

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


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_rejects_future_and_incompatible_schema() -> None:
    """Runtime instances reject unsupported versions and unowned namespaces."""

    backend = _live_backend()
    _clear_live_data(backend)
    schema_key = f"{backend.key_prefix}:schema"
    try:
        backend._direct(lambda client: client.hset(schema_key, "application_id", "other"))
        with pytest.raises(UnsupportedOperation, match="another application"):
            _live_backend()
        with pytest.raises(UnsupportedOperation, match="another application"):
            _live_backend(initialize_schema=False)

        backend._direct(lambda client: client.hset(schema_key, "application_id", redis_module._APPLICATION_ID))
        backend._direct(lambda client: client.hset(schema_key, "version", "999"))
        with pytest.raises(UnsupportedOperation, match="newer"):
            _live_backend(initialize_schema=False)
        with pytest.raises(UnsupportedOperation, match="newer"):
            _live_backend()

        backend._direct(lambda client: client.hset(schema_key, "version", "invalid"))
        with pytest.raises(UnsupportedOperation, match="version is invalid"):
            _live_backend(initialize_schema=False)

        backend._direct(lambda client: client.hset(schema_key, "version", "0"))
        with pytest.raises(UnsupportedOperation, match="No Redis migration"):
            _live_backend(initialize_schema=False)
        with _live_backend() as migrated:
            assert migrated._read_schema_identity() == {
                "application_id": redis_module._APPLICATION_ID,
                "version": str(redis_module._SCHEMA_VERSION),
            }

        backend._direct(lambda client: client.delete(schema_key))
        with pytest.raises(UnsupportedOperation, match="not initialized"):
            _live_backend(initialize_schema=False)
        with pytest.raises(UnsupportedOperation, match="without a schema record"):
            _live_backend()
    finally:
        _clear_live_data(backend, include_schema=True)
        backend.close()


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_coordinates_concurrent_initializers() -> None:
    """Concurrent constructors leave one valid current schema record."""

    first = _live_backend()
    _clear_live_data(first, include_schema=True)
    first.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        backends = list(executor.map(lambda _: _live_backend(), range(4)))
    try:
        for backend in backends:
            backend._validate_schema()
    finally:
        _clear_live_data(backends[0], include_schema=True)
        for backend in backends:
            backend.close()


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_rejects_wrong_credentials() -> None:
    """A real authentication failure uses Genie's public backend error."""

    assert _LIVE_URL is not None
    with pytest.raises(BackendError, match="authentication or authorization"):
        RedisBackend(
            _LIVE_URL,
            auth=("genie", "definitely-wrong-password"),
            key_prefix=_LIVE_PREFIX,
            connect_timeout=2.0,
            socket_timeout=2.0,
        )


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_REPLICA_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REPLICA_REASON)
def test_live_redis_backend_refuses_a_connection_that_lands_on_a_replica() -> None:
    """A real replica fails construction with the role message."""

    with pytest.raises(UnsupportedOperation, match="lands on a replica"):
        _live_replica_backend()


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_REPLICA_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REPLICA_REASON)
def test_live_redis_backend_role_check_runs_before_the_schema_check() -> None:
    """A replica is reported as a replica, not as a missing schema record.

    The empty namespace is what makes this an order test. With the role check
    off the same connection fails on the missing schema record, so the role
    message only appears when the role check runs first.
    """

    primary = _live_backend()
    try:
        _clear_live_data(primary, include_schema=True)
        _wait_for_replicated_key(f"{_LIVE_PREFIX}:schema", present=False)

        with pytest.raises(UnsupportedOperation, match="lands on a replica"):
            _live_replica_backend(initialize_schema=False)
        with pytest.raises(UnsupportedOperation, match="not initialized"):
            _live_replica_backend(verify_server_role=False, initialize_schema=False)
    finally:
        _clear_live_data(primary, include_schema=True)
        primary.close()


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_REPLICA_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REPLICA_REASON)
def test_live_redis_backend_on_a_replica_reads_replicated_data_and_refuses_writes() -> None:
    """The opt-out flag opens a replica, which reads but never writes.

    This is the behavior the flag accepts, and the reason the check is on by
    default: the replica answers reads with no signal that they may be stale,
    while every write fails.
    """

    primary = _live_backend()
    _clear_live_data(primary)
    block = primary.put_block(MemoryBlock(id="replicated", type="event", value={"seen": True}))
    # Replication keeps the order of the primary's writes, so the block key
    # arriving also means the schema record the replica backend validates has
    # arrived.
    _wait_for_replicated_key(f"{_LIVE_PREFIX}:block:{block.id}")

    replica = _live_replica_backend(verify_server_role=False, initialize_schema=False)
    try:
        assert replica.get_block(block.id) == block
        with pytest.raises(BackendReadOnly, match="read-only"):
            replica.put_block(MemoryBlock(id="rejected", type="event", value={}))
        assert replica.get_block("rejected") is None
    finally:
        replica.close()
        _clear_live_data(primary)
        primary.close()
