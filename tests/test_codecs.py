"""Tests for value codecs and codec registry behavior."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pytest

from genie import CodecRegistry, EncodedValue, JsonCodec, Memory, MemoryCodec


@dataclass(frozen=True)
class Preference:
    """Custom value used to exercise codec round-tripping."""

    topic: str
    value: str


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
