"""Tests for value codecs and codec registry behavior."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import decimal
import enum
import math
from typing import Any
import uuid

import pytest

from genie import CodecRegistry, EncodedValue, JsonCodec, Memory, MemoryCodec
from genie.codecs.stdlib import (
    BytesCodec,
    DataclassCodec,
    DatetimeCodec,
    DecimalCodec,
    EnumCodec,
    UUIDCodec,
)


@dataclass(frozen=True)
class Preference:
    """Custom value used to exercise codec round-tripping."""

    topic: str
    value: str


@dataclass(frozen=True)
class DataclassPreference:
    """Registered dataclass value used by `DataclassCodec` tests."""

    topic: str
    scores: list[int]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class UnsupportedDataclass:
    """Dataclass with a non-JSON field value."""

    values: set[int]


class Priority(str, enum.Enum):
    """Registered enum value used by `EnumCodec` tests."""

    HIGH = "high"
    LOW = "low"


class PreferenceCodec(MemoryCodec):
    """Test codec that stores `Preference` values as dictionaries."""

    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether this codec can encode the value."""

        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a preference value."""

        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value: EncodedValue) -> Any:
        """Decode a preference value."""

        return Preference(**value.data)


def test_custom_codec_round_trips_user_types() -> None:
    """Custom codecs round-trip user-level objects."""

    codecs = CodecRegistry([JsonCodec(), PreferenceCodec()])
    memory = Memory(codecs=codecs)

    block = memory.put(type="preference", value=Preference("style", "concise"))

    assert block.value == Preference("style", "concise")
    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value == Preference("style", "concise")


def test_codec_registry_reports_unknown_encoders_and_decoders() -> None:
    """The codec registry raises clear errors for unknown codecs."""

    registry = CodecRegistry()

    with pytest.raises(TypeError):
        registry.encode(object())
    with pytest.raises(TypeError):
        registry.decode(EncodedValue("application/vnd.unknown", {}))


def test_codec_registry_reports_unknown_requested_content_type() -> None:
    """Explicit missing content types fail during writes."""

    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="note", value={"safe": True}, content_type="application/vnd.missing")


def test_json_codec_rejects_non_json_compatible_values() -> None:
    """The JSON codec rejects unsupported containers and non-finite floats."""

    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="bad", value={"items": {1, 2, 3}})
    with pytest.raises(TypeError):
        memory.put(type="bad", value={"score": math.nan})
    with pytest.raises(TypeError):
        memory.put(type="bad", value=Priority.HIGH)


def test_memory_defaults_to_json_codec_only() -> None:
    """Stdlib codecs are opt-in and are not registered by default."""

    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="blob", value=b"hello")
    with pytest.raises(TypeError):
        memory.put(type="decimal", value=decimal.Decimal("1.25"))
    with pytest.raises(TypeError):
        memory.put(type="uuid", value=uuid.uuid4())


def test_bytes_codec_round_trips_base64_payloads() -> None:
    """BytesCodec stores bytes as base64 text and returns bytes to callers."""

    memory = Memory(codecs=CodecRegistry([BytesCodec(), JsonCodec()]))
    payload = b"\x00hello\xff"

    block = memory.put(type="blob", value=payload)
    raw = memory.backend.get_block(block.id)
    retrieved = memory.get(block.id)

    assert block.value == payload
    assert block.content_type == BytesCodec.content_type
    assert raw is not None
    assert isinstance(raw.value, str)
    assert retrieved is not None
    assert retrieved.value == payload


def test_stdlib_codecs_reject_malformed_encoded_payloads() -> None:
    """Stdlib codecs fail clearly when stored payloads are malformed."""

    malformed_cases = [
        (BytesCodec(), EncodedValue(BytesCodec.content_type, "not base64!"), ValueError),
        (BytesCodec(), EncodedValue(BytesCodec.content_type, 123), TypeError),
        (
            DatetimeCodec(),
            EncodedValue(DatetimeCodec.content_type, {"kind": "duration", "value": "PT1H"}),
            ValueError,
        ),
        (
            DatetimeCodec(),
            EncodedValue(DatetimeCodec.content_type, {"kind": "date", "value": 123}),
            TypeError,
        ),
        (DecimalCodec(), EncodedValue(DecimalCodec.content_type, 123), TypeError),
        (
            DecimalCodec(),
            EncodedValue(DecimalCodec.content_type, "not-a-decimal"),
            decimal.InvalidOperation,
        ),
        (UUIDCodec(), EncodedValue(UUIDCodec.content_type, "not-a-uuid"), ValueError),
        (
            DataclassCodec([DataclassPreference]),
            EncodedValue(DataclassCodec.content_type, {"type": "missing", "fields": {}}),
            TypeError,
        ),
        (
            EnumCodec([Priority]),
            EncodedValue(EnumCodec.content_type, {"type": f"{Priority.__module__}:Priority"}),
            TypeError,
        ),
        (
            EnumCodec([Priority]),
            EncodedValue(
                EnumCodec.content_type,
                {"type": f"{Priority.__module__}:Priority", "name": "MISSING"},
            ),
            KeyError,
        ),
    ]

    for codec, encoded, error_type in malformed_cases:
        with pytest.raises(error_type):
            codec.decode(encoded)


def test_datetime_codec_round_trips_datetime_date_and_time() -> None:
    """DatetimeCodec preserves concrete datetime/date/time value classes."""

    memory = Memory(codecs=CodecRegistry([DatetimeCodec(), JsonCodec()]))
    values = [
        dt.datetime(2026, 6, 30, 12, 15, tzinfo=dt.timezone.utc),
        dt.date(2026, 6, 30),
        dt.time(12, 15, 30),
    ]

    blocks = [memory.put(type="temporal", value=value) for value in values]
    retrieved_values = []
    for block in blocks:
        retrieved = memory.get(block.id)
        assert retrieved is not None
        retrieved_values.append(retrieved.value)

    assert [block.value for block in blocks] == values
    assert retrieved_values == values


def test_decimal_codec_round_trips_without_float_precision_loss() -> None:
    """DecimalCodec stores decimals as strings to preserve precision."""

    memory = Memory(codecs=CodecRegistry([DecimalCodec(), JsonCodec()]))
    value = decimal.Decimal("0.1000000000000000000000000001")

    block = memory.put(type="amount", value=value)
    raw = memory.backend.get_block(block.id)

    assert block.value == value
    assert raw is not None
    assert raw.value == str(value)


def test_uuid_codec_round_trips_uuid_values() -> None:
    """UUIDCodec stores UUIDs as canonical strings."""

    memory = Memory(codecs=CodecRegistry([UUIDCodec(), JsonCodec()]))
    value = uuid.uuid4()

    block = memory.put(type="identifier", value=value)
    raw = memory.backend.get_block(block.id)

    assert block.value == value
    assert raw is not None
    assert raw.value == str(value)


def test_dataclass_codec_round_trips_registered_dataclasses_defensively() -> None:
    """DataclassCodec round-trips registered dataclasses with copied fields."""

    codec = DataclassCodec([DataclassPreference])
    memory = Memory(codecs=CodecRegistry([codec, JsonCodec()]))
    value = DataclassPreference("style", [1], {"tags": ["concise"]})

    block = memory.put(type="preference", value=value)
    value.scores.append(2)
    value.metadata["tags"].append("mutated")
    block.value.scores.append(3)

    retrieved = memory.get(block.id)

    assert retrieved is not None
    assert retrieved.value == DataclassPreference("style", [1], {"tags": ["concise"]})


def test_dataclass_codec_requires_registered_json_compatible_types() -> None:
    """DataclassCodec rejects unregistered and non-JSON dataclass payloads."""

    registered_codec = DataclassCodec([DataclassPreference])
    unsupported_codec = DataclassCodec([UnsupportedDataclass])

    with pytest.raises(TypeError):
        registered_codec.encode(Preference("style", "concise"))
    with pytest.raises(TypeError):
        registered_codec.encode(UnsupportedDataclass({1, 2}))
    with pytest.raises(TypeError):
        unsupported_codec.encode(UnsupportedDataclass({1, 2}))


def test_enum_codec_round_trips_registered_enum_members() -> None:
    """EnumCodec stores registered enum members by type and name."""

    memory = Memory(codecs=CodecRegistry([EnumCodec([Priority]), JsonCodec()]))

    block = memory.put(type="priority", value=Priority.HIGH)
    raw = memory.backend.get_block(block.id)
    retrieved = memory.get(block.id)

    assert block.value is Priority.HIGH
    assert raw is not None
    assert raw.value["name"] == "HIGH"
    assert retrieved is not None
    assert retrieved.value is Priority.HIGH


def test_enum_codec_requires_registered_enum_types() -> None:
    """EnumCodec does not encode or decode enum types that were not registered."""

    codec = EnumCodec([Priority])
    encoded = codec.encode(Priority.LOW)

    with pytest.raises(TypeError):
        EnumCodec().encode(Priority.LOW)
    with pytest.raises(TypeError):
        EnumCodec().decode(encoded)
