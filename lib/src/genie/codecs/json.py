"""JSON-compatible default codec."""

from __future__ import annotations

import enum
import math
from copy import deepcopy
from typing import Any

from genie.codecs.base import EncodedValue

# Most levels of nested lists and dictionaries in one JSON value.
# - **Counts containers only:** `[]` and `{}` are one level each. Scalars add no level.
# - **Keeps later steps safe:** copies and backend JSON checks recurse once per level, so this bound keeps
#   them far below the Python recursion limit.
# - **Also stops cycles:** a list or dictionary that contains itself is deeper than any bound.
MAX_NESTING_DEPTH = 100


class JsonCodec:
    """Codec for JSON-compatible values.

    The codec accepts `None`, strings, booleans, integers, finite floats, lists,
    and dictionaries with string keys, nested at most `MAX_NESTING_DEPTH` levels
    deep. Values are deep-copied on encode and decode so callers cannot mutate
    stored state through returned objects.
    """

    content_type = "application/json"

    def can_encode(self, value: Any) -> bool:
        """Return whether the value is JSON-compatible.

        A value that nests too deeply still returns `True`, so `encode` can
        reject it with `ValueError` instead of the registry reporting that no
        codec handles lists or dictionaries.

        Args:
            value: User-level value.

        Returns:
            `True` when the value can be encoded by this codec.
        """

        return not isinstance(_json_error(value), TypeError)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a JSON-compatible value.

        Args:
            value: User-level JSON-compatible value.

        Returns:
            Encoded value with the JSON content type.

        Raises:
            TypeError: If the value is not JSON-compatible.
            ValueError: If lists and dictionaries nest more than
                `MAX_NESTING_DEPTH` levels deep.
        """

        error = _json_error(value)
        if error is not None:
            raise error
        return EncodedValue(content_type=self.content_type, data=deepcopy(value))

    def decode(self, value: EncodedValue) -> Any:
        """Decode a JSON-compatible encoded value.

        Args:
            value: Encoded value.

        Returns:
            A deep copy of the encoded data.

        Raises:
            TypeError: If the encoded value was produced by another codec, or
                the payload is not JSON-compatible or nests more than
                `MAX_NESTING_DEPTH` levels deep.
        """

        if not isinstance(value, EncodedValue):
            raise TypeError("JsonCodec can decode only EncodedValue instances.")
        if value.content_type != self.content_type:
            raise TypeError(f"JsonCodec cannot decode {value.content_type!r}.")
        error = _json_error(value.data)
        if error is not None:
            raise TypeError("JsonCodec payload is not JSON-compatible.") from error
        return deepcopy(value.data)


def _json_error(value: Any) -> TypeError | ValueError | None:
    """Return the first strict JSON problem in a value, or `None`.

    - **`TypeError`:** a part of the value is not strict JSON data.
    - **`ValueError`:** lists and dictionaries nest more than `MAX_NESTING_DEPTH` levels deep.
    - **No recursion:** the walk uses its own stack and stops at the bound, so a deep value cannot raise `RecursionError` here.
    - **Speed:** only lists and dictionaries go on the stack. Scalars are checked in place.
    """

    if not _is_container(value):
        return _scalar_error(value)
    pending: list[tuple[list[Any] | dict[Any, Any], int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth == MAX_NESTING_DEPTH:
            return ValueError(f"Lists and dictionaries nest more than {MAX_NESTING_DEPTH} levels deep.")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                return TypeError("Non-string dictionary keys are not JSON-compatible.")
            children = item.values()
        else:
            children = item
        for child in children:
            if _is_container(child):
                pending.append((child, depth + 1))
                continue
            error = _scalar_error(child)
            if error is not None:
                return error
    return None


def _is_container(value: Any) -> bool:
    """Return whether a value is a list or dictionary that the walk descends into."""

    return isinstance(value, list | dict) and not isinstance(value, enum.Enum)


def _scalar_error(value: Any) -> TypeError | None:
    """Return a `TypeError` for a non-container value that is not strict JSON, or `None`."""

    if isinstance(value, enum.Enum):
        return TypeError(f"{type(value).__name__!r} is not JSON-compatible.")
    if value is None or isinstance(value, str | bool | int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else TypeError("Non-finite floats are not JSON-compatible.")
    return TypeError(f"{type(value).__name__!r} is not JSON-compatible.")
