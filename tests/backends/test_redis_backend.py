"""Unit and opt-in live tests for the official-client Redis backend."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Condition, Event, RLock
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
)


class _FakeWatchError(Exception):
    """Stand-in for the client's optimistic conflict error."""


class _FakeNoScriptError(Exception):
    """Stand-in for the client's missing cached script error."""


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
        ({"url": "redis://example", "auth": ["user", "password"]}, TypeError, "auth must be"),
        ({"url": "redis://example", "verify_connectivity": 1}, TypeError, "verify_connectivity must be"),
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

    class FakeModule:
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
    )
    backend.close()

    assert calls == {
        "url": "rediss://example.test:6379/2",
        "options": {
            "decode_responses": True,
            "socket_connect_timeout": 4.5,
            "socket_timeout": None,
            "username": "genie",
            "password": "secret",
        },
        "pinged": True,
        "closed": True,
    }


@pytest.mark.parametrize(
    ("verify_eviction_policy", "expected_sections"),
    [(True, ["memory"]), (False, [])],
)
def test_redis_backend_checks_the_eviction_policy_before_it_touches_the_schema(
    monkeypatch: pytest.MonkeyPatch,
    verify_eviction_policy: bool,
    expected_sections: list[str],
) -> None:
    """The eviction check runs before schema work, and the flag can skip it."""

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

    class FakeModule:
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
            RedisBackend("redis://example", verify_eviction_policy=True)
        assert steps == []
    else:
        backend = RedisBackend("redis://example", verify_eviction_policy=False)
        backend.close()
        assert steps == ["schema"]
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

    class FakeModule:
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

        def incr(self, key: str) -> None:
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


def test_redis_ancestors_runs_as_one_read_only_script() -> None:
    """A supported server returns the whole parent chain from one script."""

    root = MemoryBlock(id="root", type="project", value={})
    middle = MemoryBlock(id="middle", parent_id=root.id, type="step", value={})

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [_stored_payload(middle, 1), _stored_payload(root, 0)]

        def get(self, key: str) -> None:
            raise AssertionError("a scripted read must not read the mutation guard")

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.ancestors("leaf") == [middle, root]
    assert client.calls == [(redis_module._LUA_DIGESTS["ancestors"], 1, "genie:block:leaf", "genie", "leaf")]


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
        (redis_module._LUA_DIGESTS["list_links"], 2, "genie:outlinks:root", "genie:inlinks:root", "genie", "root", "both"),
    ]


def test_redis_list_links_filters_the_relation_after_a_script() -> None:
    """The relation filter narrows the reply without a second script call."""

    outgoing = MemoryLink("root", "child", "owns", id="outgoing")
    incoming = MemoryLink("other", "root", "notes", id="incoming")

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls = 0

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls += 1
            return [_stored_link_payload(outgoing, 0), _stored_link_payload(incoming, 1)]

        def close(self) -> None:
            return None

    client = ScriptedClient()
    backend = _detached_backend(client, readonly_scripts=True)

    assert backend.list_links("root", relation="notes", direction="both") == [incoming]
    assert client.calls == 1


def test_redis_list_links_falls_back_without_read_only_script_support() -> None:
    """A server without scripting keeps the guarded two-command read."""

    link = MemoryLink("root", "child", "owns", id="link")

    class GuardedClient:
        def __init__(self) -> None:
            self.guard_reads = 0

        def get(self, key: str) -> str:
            assert key == "genie:mutations"
            self.guard_reads += 1
            return "5"

        def zrange(self, key: str, start: int, stop: int) -> list[str]:
            assert (key, start, stop) == ("genie:outlinks:root", 0, -1)
            return ["link", "removed"]

        def mget(self, keys: list[str]) -> list[str | None]:
            assert keys == ["genie:link:link", "genie:link:removed"]
            return [_stored_link_payload(link, 0), None]

        def close(self) -> None:
            return None

    client = GuardedClient()
    backend = _detached_backend(client)

    assert backend.list_links("root") == [link]
    assert client.guard_reads == 2


def test_redis_temporal_page_runs_as_one_read_only_script() -> None:
    """A supported server returns one entry page from one script."""

    first = TemporalEntry("collection", "first", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = TemporalEntry("collection", "second", occurred_at=datetime(2026, 2, 1, tzinfo=UTC))

    class ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            self.calls.append((digest, numkeys, *arguments))
            return [first.block_id, _stored_temporal_payload(first), second.block_id, _stored_temporal_payload(second)]

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
        def evalsha_ro(self, digest: str, numkeys: int, *arguments: str) -> list[str]:
            return ["first"]

        def close(self) -> None:
            return None

    backend = _detached_backend(ScriptedClient(), readonly_scripts=True)

    with pytest.raises(BackendCorruption, match="unpaired reply"):
        backend.list_temporal_entries("collection")


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
_LIVE_CLIENT_AVAILABLE = importlib.util.find_spec("redis") is not None
_LIVE_REASON = "set GENIE_REDIS_URL and install the redis extra to run live Redis tests"


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
    KeyedStoreContract,
    SequenceStoreContract,
    TemporalStoreContract,
    TemporalIndexContract,
    LinkStoreContract,
    AcyclicLinkStoreContract,
):
    """Run every reusable capability contract against a live Redis server."""

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
    """Scripted depth reads stay correct and never report contention."""

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


@pytest.mark.skipif(not (_LIVE_URL and _LIVE_CLIENT_AVAILABLE), reason=_LIVE_REASON)
def test_live_redis_backend_leaves_no_orphan_keys_after_deletion() -> None:
    """A deleted subtree removes every index and reverse-index key it owned."""

    with _live_backend() as backend:
        _clear_live_data(backend)
        root = backend.put_block(MemoryBlock(id="root", type="root", value={}))
        child = backend.put_block(MemoryBlock(id="child", parent_id=root.id, type="child", value={}))
        outside = backend.put_block(MemoryBlock(id="outside", type="other", value={}))
        backend.add_link(MemoryLink(child.id, outside.id, "cites"))
        backend.add_link(MemoryLink(outside.id, child.id, "references"))
        backend.bind_key(KeyBinding(root.id, MemoryAddress((), "child"), child.id))
        backend.bind_key(KeyBinding(outside.id, MemoryAddress((), "child"), child.id))
        backend.append_log_entry(root.id, child.id)
        backend.append_log_entry(outside.id, child.id)
        backend.add_temporal_entry(TemporalEntry(root.id, child.id, occurred_at=child.created_at))
        backend.add_temporal_entry(TemporalEntry(outside.id, child.id, occurred_at=child.created_at))

        backend.delete_block(root.id)

        remaining = sorted(backend._direct(lambda client: client.keys(f"{backend.key_prefix}:*")))
        expected = sorted(
            [
                f"{backend.key_prefix}:schema",
                f"{backend.key_prefix}:mutations",
                f"{backend.key_prefix}:order:blocks",
                f"{backend.key_prefix}:order:links",
                f"{backend.key_prefix}:blocks",
                f"{backend.key_prefix}:block:{outside.id}",
                f"{backend.key_prefix}:logcounter:{outside.id}",
            ]
        )
        assert remaining == expected
        assert backend.list_links(outside.id, direction="both") == []
        assert backend.list_keys(outside.id) == []
        assert backend.list_log_entries(outside.id) == []
        assert backend.list_temporal_entries(outside.id) == []
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
