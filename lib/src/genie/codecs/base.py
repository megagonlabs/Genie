"""Codec interfaces that change user values into payloads that are safe to store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EncodedValue:
    """A value after a codec encodes it.

    Attributes:
        content_type: Codec content type. The registry uses it to find the codec for decode.
        data: The user value in a form that is safe to store.

    Raises:
        TypeError: If `content_type` is not a string.
        ValueError: If `content_type` is empty.
    """

    content_type: str
    data: Any

    def __post_init__(self) -> None:
        """Make sure that the content type is a non-empty string, so the registry can find the codec."""

        _require_content_type(self.content_type)


@runtime_checkable
class MemoryCodec(Protocol):
    """Protocol for custom codecs of memory values."""

    content_type: str

    def can_encode(self, value: Any) -> bool:
        """Return whether this codec can encode a value.

        Args:
            value: User-level value that the caller gives to `Memory.put` or `Memory.set`.

        Returns:
            `True` when `encode` can accept the value. `encode` can still raise
            `ValueError` for a bad value of a kind that this codec accepts. So
            the caller gets that error and not "no codec registered".
        """

        ...

    def encode(self, value: Any) -> EncodedValue:
        """Encode a user value into a payload that is safe to store.

        Args:
            value: User-level value.

        Returns:
            Encoded value with the content type of this codec.
        """

        ...

    def decode(self, value: EncodedValue) -> Any:
        """Decode a stored value back into a user-level value.

        Args:
            value: Encoded value from a compatible codec.

        Returns:
            Decoded user-level value.
        """

        ...


class CodecRegistry:
    """Registry that selects the codec for each encode and decode operation."""

    def __init__(self, codecs: list[MemoryCodec] | None = None) -> None:
        """Create a codec registry.

        Args:
            codecs: Optional codecs. The registry registers them in this order.
        """

        self._codecs: dict[str, MemoryCodec] = {}
        for codec in codecs or ():
            self.register(codec)

    def register(self, codec: MemoryCodec) -> None:
        """Register a codec, or replace the codec that has the same content type.

        - **Order:** `encode` without a `content_type` tries codecs in registration order.
        - **Replacement:** a replacement codec keeps the position of the codec that it replaces.

        Args:
            codec: Codec implementation.

        Raises:
            TypeError: If `codec` is a class and not an instance, does not
                implement `MemoryCodec`, or has a content type that is not a
                string.
            ValueError: If the codec content type is empty.
        """

        if isinstance(codec, type) or not isinstance(codec, MemoryCodec):
            raise TypeError("codec must implement the MemoryCodec protocol.")
        _require_content_type(codec.content_type)
        self._codecs[codec.content_type] = codec

    def encode(self, value: Any, content_type: str | None = None) -> EncodedValue:
        """Encode a value with the codec for `content_type`, or with the first codec that accepts the value.

        Args:
            value: User-level value.
            content_type: Optional content type of the codec to use.

        Returns:
            Encoded value.

        Raises:
            TypeError: If `content_type` is not a string, no registered codec
                matches, the selected codec rejects the value type, or a codec
                returns an object that is not an `EncodedValue`.
            ValueError: If `content_type` is empty, a codec returns a different
                content type, or the selected codec rejects a bad value. An
                example is JSON that nests more than `MAX_NESTING_DEPTH` levels.
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
        """Decode a value with the codec for its content type.

        Args:
            value: Encoded value to decode.

        Returns:
            Decoded user-level value.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, or no registered
                codec matches its content type. The selected codec can also
                raise its own errors.
        """

        if not isinstance(value, EncodedValue):
            raise TypeError("value must be an EncodedValue.")
        codec = self._codecs.get(value.content_type)
        if codec is None:
            raise TypeError(f"No Genie codec registered for {value.content_type!r}.")
        return codec.decode(value)


def _require_content_type(content_type: object) -> None:
    """Raise an error if a codec content type is not a non-empty string."""

    if not isinstance(content_type, str):
        raise TypeError("content_type must be a string.")
    if not content_type:
        raise ValueError("content_type must not be empty.")


def _require_codec_result(codec: MemoryCodec, value: object) -> EncodedValue:
    """Check the encoded value that a custom codec returns, and return it."""

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
