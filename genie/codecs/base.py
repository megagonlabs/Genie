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
    """

    content_type: str
    data: Any


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
        """

        self._codecs[codec.content_type] = codec

    def encode(self, value: Any, content_type: str | None = None) -> EncodedValue:
        """Encode a value using an explicit or inferred codec.

        Args:
            value: User-level value.
            content_type: Optional explicit codec content type.

        Returns:
            Encoded value.

        Raises:
            TypeError: If no matching codec is registered.
        """

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
        """Decode a value using its content type.

        Args:
            value: Encoded value to decode.

        Returns:
            Decoded user-level value.

        Raises:
            TypeError: If no matching codec is registered.
        """

        codec = self._codecs.get(value.content_type)
        if codec is None:
            raise TypeError(f"No Genie codec registered for {value.content_type!r}.")
        return codec.decode(value)
