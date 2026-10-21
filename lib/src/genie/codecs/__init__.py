"""Codec interfaces for memory values, and the built-in codecs."""

from genie.codecs.base import CodecRegistry, EncodedValue, MemoryCodec
from genie.codecs.json import MAX_NESTING_DEPTH, JsonCodec
from genie.codecs.stdlib import (
    BytesCodec,
    DataclassCodec,
    DatetimeCodec,
    DecimalCodec,
    EnumCodec,
    UUIDCodec,
)

__all__ = [
    "MAX_NESTING_DEPTH",
    "BytesCodec",
    "CodecRegistry",
    "DataclassCodec",
    "DatetimeCodec",
    "DecimalCodec",
    "EncodedValue",
    "EnumCodec",
    "JsonCodec",
    "MemoryCodec",
    "UUIDCodec",
]
