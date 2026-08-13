"""Codec interfaces for translating user values to storage-safe payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EncodedValue:
    """A value after codec encoding.

    Attributes:
        content_type: Codec content type used to decode the value later.
        data: Storage-safe representation of the user value.

    Raises:
        TypeError: If `content_type` is not a string.
        ValueError: If `content_type` is empty.
    """

    content_type: str
    data: Any

    def __post_init__(self) -> None:
        """Require a non-empty content type for reliable codec dispatch."""

        _require_content_type(self.content_type)


@runtime_checkable
class MemoryCodec(Protocol):
    """Protocol for custom memory value codecs."""

    content_type: str

    def can_encode(self, value: Any) -> bool:
        """Return whether this codec can encode a value.

        Args:
            value: User-level value passed to `Memory.put` or `Memory.set`.

        Returns:
            `True` when `encode` can handle the value.
        """

        ...

    def encode(self, value: Any) -> EncodedValue:
        """Encode a user value into a storage-safe payload.

        Args:
            value: User-level value.

        Returns:
            Encoded value carrying this codec's content type.
        """

        ...

    def decode(self, value: EncodedValue) -> Any:
        """Decode a stored value back into a user-level value.

        Args:
            value: Encoded value produced by a compatible codec.

        Returns:
            Decoded user-level value.
        """

        ...


class CodecRegistry:
    """Registry that chooses codecs for encode/decode operations."""

    def __init__(self, codecs: list[MemoryCodec] | None = None) -> None:
        """Create a codec registry.

        Args:
            codecs: Optional codecs to register in order.
        """

        self._codecs: dict[str, MemoryCodec] = {}
        for codec in codecs or ():
            self.register(codec)

    def register(self, codec: MemoryCodec) -> None:
        """Register or replace a codec by content type.

        Args:
            codec: Codec implementation.

        Raises:
            TypeError: If `codec` does not implement `MemoryCodec`.
            ValueError: If the codec content type is empty.
        """

        if isinstance(codec, type) or not isinstance(codec, MemoryCodec):
            raise TypeError("codec must implement the MemoryCodec protocol.")
        _require_content_type(codec.content_type)
        self._codecs[codec.content_type] = codec

    def encode(self, value: Any, content_type: str | None = None) -> EncodedValue:
        """Encode a value using an explicit or inferred codec.

        Args:
            value: User-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Encoded value.

        Raises:
            TypeError: If no matching codec is registered or a codec returns
                something other than `EncodedValue`.
            ValueError: If a codec returns a different content type.
        """

        if content_type is not None:
            _require_content_type(content_type)
            codec = self._codecs.get(content_type)
            if codec is None:
                raise TypeError(f"No Genie codec registered for {content_type!r}.")
            return _require_codec_result(codec, codec.encode(value))
        for codec in self._codecs.values():
            if codec.can_encode(value):
                return _require_codec_result(codec, codec.encode(value))
        raise TypeError(
            f"No Genie codec registered for value of type {type(value).__name__!r}."
        )

    def decode(self, value: EncodedValue) -> Any:
        """Decode a value using its content type.

        Args:
            value: Encoded value to decode.

        Returns:
            Decoded user-level value.

        Raises:
            TypeError: If no matching codec is registered.
        """

        if not isinstance(value, EncodedValue):
            raise TypeError("value must be an EncodedValue.")
        codec = self._codecs.get(value.content_type)
        if codec is None:
            raise TypeError(f"No Genie codec registered for {value.content_type!r}.")
        return codec.decode(value)


def _require_content_type(content_type: object) -> None:
    """Require a non-empty string codec content type."""

    if not isinstance(content_type, str):
        raise TypeError("content_type must be a string.")
    if not content_type:
        raise ValueError("content_type must not be empty.")


def _require_codec_result(codec: MemoryCodec, value: object) -> EncodedValue:
    """Validate the encoded value returned by a custom codec."""

    if not isinstance(value, EncodedValue):
        raise TypeError(
            f"Codec {codec.content_type!r} must return an EncodedValue from encode()."
        )
    if value.content_type != codec.content_type:
        raise ValueError(
            f"Codec {codec.content_type!r} returned content type "
            f"{value.content_type!r}."
        )
    return value
