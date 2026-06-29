from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EncodedValue:
    content_type: str
    data: Any


@runtime_checkable
class MemoryCodec(Protocol):
    content_type: str

    def can_encode(self, value: Any) -> bool: ...

    def encode(self, value: Any) -> EncodedValue: ...

    def decode(self, value: EncodedValue) -> Any: ...


class CodecRegistry:
    def __init__(self, codecs: list[MemoryCodec] | None = None) -> None:
        self._codecs: dict[str, MemoryCodec] = {}
        for codec in codecs or ():
            self.register(codec)

    def register(self, codec: MemoryCodec) -> None:
        self._codecs[codec.content_type] = codec

    def encode(self, value: Any, content_type: str | None = None) -> EncodedValue:
        if content_type:
            codec = self._codecs.get(content_type)
            if codec is None:
                raise TypeError(f"No Genie codec registered for {content_type!r}.")
            return codec.encode(value)
        for codec in self._codecs.values():
            if codec.can_encode(value):
                return codec.encode(value)
        raise TypeError(
            f"No Genie codec registered for value of type {type(value).__name__!r}."
        )

    def decode(self, value: EncodedValue) -> Any:
        codec = self._codecs.get(value.content_type)
        if codec is None:
            raise TypeError(f"No Genie codec registered for {value.content_type!r}.")
        return codec.decode(value)
