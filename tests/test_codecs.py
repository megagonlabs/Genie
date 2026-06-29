from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pytest

from genie import CodecRegistry, EncodedValue, JsonCodec, Memory, MemoryCodec


@dataclass(frozen=True)
class Preference:
    topic: str
    value: str


class PreferenceCodec(MemoryCodec):
    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value: EncodedValue) -> Any:
        return Preference(**value.data)


def test_custom_codec_round_trips_user_types() -> None:
    codecs = CodecRegistry([JsonCodec(), PreferenceCodec()])
    memory = Memory(codecs=codecs)

    block = memory.put(type="preference", value=Preference("style", "concise"))

    assert block.value == Preference("style", "concise")
    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value == Preference("style", "concise")


def test_codec_registry_reports_unknown_encoders_and_decoders() -> None:
    registry = CodecRegistry()

    with pytest.raises(TypeError):
        registry.encode(object())
    with pytest.raises(TypeError):
        registry.decode(EncodedValue("application/vnd.unknown", {}))


def test_codec_registry_reports_unknown_requested_content_type() -> None:
    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="note", value={"safe": True}, content_type="application/vnd.missing")


def test_json_codec_rejects_non_json_compatible_values() -> None:
    memory = Memory()

    with pytest.raises(TypeError):
        memory.put(type="bad", value={"items": {1, 2, 3}})
    with pytest.raises(TypeError):
        memory.put(type="bad", value={"score": math.nan})
