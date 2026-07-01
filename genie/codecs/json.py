"""JSON-compatible default codec."""

from __future__ import annotations

from copy import deepcopy
import enum
import math
from typing import Any

from genie.codecs.base import EncodedValue


class JsonCodec:
    """Codec for JSON-compatible values.

    The codec accepts `None`, strings, booleans, integers, finite floats, lists,
    and dictionaries with string keys. Values are deep-copied on encode and
    decode so callers cannot mutate stored state through returned objects.
    """

    content_type = "application/json"

    def can_encode(self, value: Any) -> bool:
        """Return whether the value is JSON-compatible.

        Args:
            value: User-level value.

        Returns:
            `True` when the value can be encoded by this codec.
        """

        return _is_json_compatible(value)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a JSON-compatible value.

        Args:
            value: User-level JSON-compatible value.

        Returns:
            Encoded value with the JSON content type.

        Raises:
            TypeError: If the value is not JSON-compatible.
        """

        if not self.can_encode(value):
            raise TypeError(f"{type(value).__name__!r} is not JSON-compatible.")
        return EncodedValue(content_type=self.content_type, data=deepcopy(value))

    def decode(self, value: EncodedValue) -> Any:
        """Decode a JSON-compatible encoded value.

        Args:
            value: Encoded value.

        Returns:
            A deep copy of the encoded data.

        Raises:
            TypeError: If the encoded value was produced by another codec.
        """

        if value.content_type != self.content_type:
            raise TypeError(f"JsonCodec cannot decode {value.content_type!r}.")
        return deepcopy(value.data)


def _is_json_compatible(value: Any) -> bool:
    """Return whether a value can be represented as strict JSON data."""

    if isinstance(value, enum.Enum):
        return False
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(val) for key, val in value.items())
    return False
