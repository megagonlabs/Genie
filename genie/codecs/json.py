from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from genie.codecs.base import EncodedValue


class JsonCodec:
    content_type = "application/json"

    def can_encode(self, value: Any) -> bool:
        return _is_json_compatible(value)

    def encode(self, value: Any) -> EncodedValue:
        if not self.can_encode(value):
            raise TypeError(f"{type(value).__name__!r} is not JSON-compatible.")
        return EncodedValue(content_type=self.content_type, data=deepcopy(value))

    def decode(self, value: EncodedValue) -> Any:
        if value.content_type != self.content_type:
            raise TypeError(f"JsonCodec cannot decode {value.content_type!r}.")
        return deepcopy(value.data)


def _is_json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(val) for key, val in value.items())
    return False
