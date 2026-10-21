"""Codecs for common values from the Python standard library, with no third-party dependencies."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as _dt
import decimal
import enum
import uuid
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from genie.codecs.base import EncodedValue
from genie.codecs.json import MAX_NESTING_DEPTH, _json_error


class BytesCodec:
    """Codec for `bytes` values, with base64 text payloads."""

    content_type = "application/vnd.genie.bytes+base64"

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is a bytes object.

        Args:
            value: User-level value.

        Returns:
            `True` when this codec can encode the value.
        """

        return isinstance(value, bytes)

    def encode(self, value: Any) -> EncodedValue:
        """Encode bytes into base64 ASCII text.

        Args:
            value: Bytes value.

        Returns:
            Encoded base64 payload.

        Raises:
            TypeError: If `value` is not bytes.
        """

        if not isinstance(value, bytes):
            raise TypeError("BytesCodec can encode only bytes.")
        data = base64.b64encode(value).decode("ascii")
        return EncodedValue(self.content_type, data)

    def decode(self, value: EncodedValue) -> bytes:
        """Decode a base64 payload back into bytes.

        Args:
            value: Encoded value from `BytesCodec`.

        Returns:
            Decoded bytes.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, or its content type
                or payload shape is not valid.
            ValueError: If the payload is not valid base64.
        """

        _require_content_type("BytesCodec", value, self.content_type)
        if not isinstance(value.data, str):
            raise TypeError("BytesCodec payload must be a base64 string.")
        try:
            return base64.b64decode(value.data.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError("BytesCodec payload is not valid base64.") from exc


class DatetimeCodec:
    """Codec for `datetime`, `date`, and `time` values."""

    content_type = "application/vnd.genie.datetime+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is a supported datetime type.

        Args:
            value: User-level value.

        Returns:
            `True` for `datetime.datetime`, `datetime.date`, and
            `datetime.time` instances.
        """

        return isinstance(value, (_dt.datetime, _dt.date, _dt.time))

    def encode(self, value: Any) -> EncodedValue:
        """Encode a datetime-like value as ISO-8601 JSON data.

        Args:
            value: Datetime-like value.

        Returns:
            Encoded payload with the concrete kind and the ISO text.

        Raises:
            TypeError: If `value` is not a supported datetime type.
        """

        if isinstance(value, _dt.datetime):
            kind = "datetime"
        elif isinstance(value, _dt.date):
            kind = "date"
        elif isinstance(value, _dt.time):
            kind = "time"
        else:
            raise TypeError("DatetimeCodec can encode only datetime, date, or time values.")
        return EncodedValue(self.content_type, {"kind": kind, "value": value.isoformat()})

    def decode(self, value: EncodedValue) -> _dt.datetime | _dt.date | _dt.time:
        """Decode ISO-8601 JSON data into a datetime-like value.

        Args:
            value: Encoded value from `DatetimeCodec`.

        Returns:
            `datetime.datetime`, `datetime.date`, or `datetime.time`.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, or its content type
                or payload shape is not valid.
            ValueError: If the kind is unknown or the ISO text is not valid.
        """

        _require_content_type("DatetimeCodec", value, self.content_type)
        data = _require_mapping_payload("DatetimeCodec", value.data)
        kind = data.get("kind")
        text = data.get("value")
        if not isinstance(kind, str) or not isinstance(text, str):
            raise TypeError("DatetimeCodec payload must contain string 'kind' and 'value'.")
        if kind == "datetime":
            return _dt.datetime.fromisoformat(text)
        if kind == "date":
            return _dt.date.fromisoformat(text)
        if kind == "time":
            return _dt.time.fromisoformat(text)
        raise ValueError(f"Unsupported datetime payload kind {kind!r}.")


class DecimalCodec:
    """Codec for `decimal.Decimal` values."""

    content_type = "application/vnd.genie.decimal+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is a decimal.

        Args:
            value: User-level value.

        Returns:
            `True` for `decimal.Decimal` values.
        """

        return isinstance(value, decimal.Decimal)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a decimal as a string that keeps its full precision.

        Args:
            value: Decimal value.

        Returns:
            Encoded decimal payload.

        Raises:
            TypeError: If `value` is not a decimal.
        """

        if not isinstance(value, decimal.Decimal):
            raise TypeError("DecimalCodec can encode only decimal.Decimal values.")
        return EncodedValue(self.content_type, str(value))

    def decode(self, value: EncodedValue) -> decimal.Decimal:
        """Decode a decimal string into a decimal with the same precision.

        Args:
            value: Encoded value from `DecimalCodec`.

        Returns:
            Decoded decimal.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, or its content type
                or payload shape is not valid.
            decimal.InvalidOperation: If the decimal text is not valid.
        """

        _require_content_type("DecimalCodec", value, self.content_type)
        if not isinstance(value.data, str):
            raise TypeError("DecimalCodec payload must be a string.")
        return decimal.Decimal(value.data)


class UUIDCodec:
    """Codec for `uuid.UUID` values."""

    content_type = "application/vnd.genie.uuid+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is a UUID.

        Args:
            value: User-level value.

        Returns:
            `True` for `uuid.UUID` values.
        """

        return isinstance(value, uuid.UUID)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a UUID as canonical text.

        Args:
            value: UUID value.

        Returns:
            Encoded UUID payload.

        Raises:
            TypeError: If `value` is not a UUID.
        """

        if not isinstance(value, uuid.UUID):
            raise TypeError("UUIDCodec can encode only uuid.UUID values.")
        return EncodedValue(self.content_type, str(value))

    def decode(self, value: EncodedValue) -> uuid.UUID:
        """Decode UUID text into a `uuid.UUID`.

        Args:
            value: Encoded value from `UUIDCodec`.

        Returns:
            Decoded UUID.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, or its content type
                or payload shape is not valid.
            ValueError: If the UUID text is not valid.
        """

        _require_content_type("UUIDCodec", value, self.content_type)
        if not isinstance(value.data, str):
            raise TypeError("UUIDCodec payload must be a string.")
        return uuid.UUID(value.data)


class DataclassCodec:
    """Codec for registered dataclass types.

    - **No import on decode:** the codec does not import classes by name.
    - **Registered types only:** callers register trusted dataclass types before use.
    - **Type key:** each encoded payload has a stable `module:qualname` key. The
      key selects one of the registered classes.
    """

    content_type = "application/vnd.genie.dataclass+json"

    def __init__(self, types: Iterable[type[Any]] = ()) -> None:
        """Create a dataclass codec.

        Args:
            types: Trusted dataclass types that this codec can round-trip.
        """

        self._types: dict[str, type[Any]] = {}
        for type_ in types:
            self.register_type(type_)

    def register_type(self, type_: type[Any]) -> None:
        """Register a dataclass type for later encode and decode operations.

        A second registration of the same type has no effect.

        Args:
            type_: Dataclass type to register.

        Raises:
            TypeError: If `type_` is not a dataclass type.
            ValueError: If another type already uses the same qualified key.
        """

        if not isinstance(type_, type) or not dataclasses.is_dataclass(type_):
            raise TypeError("DataclassCodec.register_type requires a dataclass type.")
        key = _type_key(type_)
        existing = self._types.get(key)
        if existing is not None and existing is not type_:
            raise ValueError(f"Dataclass key {key!r} is already registered.")
        self._types[key] = type_

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is an instance of a registered dataclass.

        Args:
            value: User-level value.

        Returns:
            `True` when `value` is a registered dataclass instance.
        """

        if isinstance(value, type) or not dataclasses.is_dataclass(value):
            return False
        return _type_key(type(value)) in self._types

    def encode(self, value: Any) -> EncodedValue:
        """Encode a registered dataclass instance.

        - **Fields:** the codec encodes only the fields with `init=True`.
        - **Field values:** the codec accepts only JSON-compatible field values.
          So the encoded payload stays portable across backend adapters.
        - **Copies:** the codec copies lists and dictionaries into plain `list`
          and `dict` objects.

        Args:
            value: Registered dataclass instance.

        Returns:
            Encoded dataclass payload.

        Raises:
            TypeError: If `value` is not an instance of a registered
                dataclass, or a field value is not JSON-compatible.
            ValueError: If a field value nests lists and dictionaries more
                than `MAX_NESTING_DEPTH` levels deep.
        """

        if isinstance(value, type) or not dataclasses.is_dataclass(value):
            raise TypeError("DataclassCodec can encode only dataclass instances.")
        key = _type_key(type(value))
        if key not in self._types:
            raise TypeError(f"Dataclass type {key!r} is not registered.")
        fields: dict[str, Any] = {}
        for field in dataclasses.fields(value):
            if not field.init:
                continue
            try:
                fields[field.name] = _copy_json_value(getattr(value, field.name))
            except TypeError as exc:
                raise TypeError(
                    f"Dataclass field {field.name!r} is not JSON-compatible."
                ) from exc
            except ValueError as exc:
                raise ValueError(
                    f"Dataclass field {field.name!r} nests more than {MAX_NESTING_DEPTH} levels deep."
                ) from exc
        return EncodedValue(self.content_type, {"type": key, "fields": fields})

    def decode(self, value: EncodedValue) -> Any:
        """Decode a registered dataclass instance.

        Args:
            value: Encoded value from `DataclassCodec`.

        Returns:
            Dataclass instance.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, its content type or
                payload shape is not valid, or the type is not registered. Also
                if a stored field value is not JSON-compatible, or the stored
                fields do not match the dataclass constructor.
            ValueError: If a stored field value nests lists and dictionaries
                more than `MAX_NESTING_DEPTH` levels deep.
        """

        _require_content_type("DataclassCodec", value, self.content_type)
        data = _require_mapping_payload("DataclassCodec", value.data)
        type_key = data.get("type")
        fields = data.get("fields")
        if not isinstance(type_key, str) or not isinstance(fields, dict):
            raise TypeError("DataclassCodec payload must contain 'type' and 'fields'.")
        type_ = self._types.get(type_key)
        if type_ is None:
            raise TypeError(f"Dataclass type {type_key!r} is not registered.")
        # Check each field value alone, as `encode` does. Then the `fields` wrapper does not add a level.
        return type_(**{name: _copy_json_value(item) for name, item in fields.items()})


class EnumCodec:
    """Codec for registered `enum.Enum` types."""

    content_type = "application/vnd.genie.enum+json"

    def __init__(self, types: Iterable[type[enum.Enum]] = ()) -> None:
        """Create an enum codec.

        Args:
            types: Trusted enum types that this codec can round-trip.
        """

        self._types: dict[str, type[enum.Enum]] = {}
        for type_ in types:
            self.register_type(type_)

    def register_type(self, type_: type[enum.Enum]) -> None:
        """Register an enum type for later encode and decode operations.

        A second registration of the same type has no effect.

        Args:
            type_: Enum type to register.

        Raises:
            TypeError: If `type_` is not an enum type.
            ValueError: If another type already uses the same qualified key.
        """

        if not isinstance(type_, type) or not issubclass(type_, enum.Enum):
            raise TypeError("EnumCodec.register_type requires an enum type.")
        key = _type_key(type_)
        existing = self._types.get(key)
        if existing is not None and existing is not type_:
            raise ValueError(f"Enum key {key!r} is already registered.")
        self._types[key] = type_

    def can_encode(self, value: Any) -> bool:
        """Return whether `value` is an instance of a registered enum.

        Args:
            value: User-level value.

        Returns:
            `True` when `value` is a registered enum member.
        """

        return isinstance(value, enum.Enum) and _type_key(type(value)) in self._types

    def encode(self, value: Any) -> EncodedValue:
        """Encode a registered enum member by name.

        Args:
            value: Registered enum member.

        Returns:
            Encoded enum payload.

        Raises:
            TypeError: If `value` is not a registered enum member.
        """

        if not isinstance(value, enum.Enum):
            raise TypeError("EnumCodec can encode only enum.Enum members.")
        key = _type_key(type(value))
        if key not in self._types:
            raise TypeError(f"Enum type {key!r} is not registered.")
        return EncodedValue(self.content_type, {"type": key, "name": value.name})

    def decode(self, value: EncodedValue) -> enum.Enum:
        """Decode a registered enum member.

        Args:
            value: Encoded value from `EnumCodec`.

        Returns:
            Enum member.

        Raises:
            TypeError: If `value` is not an `EncodedValue`, its content type or
                payload shape is not valid, or the type is not registered.
            KeyError: If the enum has no member with the stored name.
        """

        _require_content_type("EnumCodec", value, self.content_type)
        data = _require_mapping_payload("EnumCodec", value.data)
        type_key = data.get("type")
        name = data.get("name")
        if not isinstance(type_key, str) or not isinstance(name, str):
            raise TypeError("EnumCodec payload must contain string 'type' and 'name'.")
        type_ = self._types.get(type_key)
        if type_ is None:
            raise TypeError(f"Enum type {type_key!r} is not registered.")
        return type_[name]


def _require_content_type(codec_name: str, value: EncodedValue, expected: str) -> None:
    """Raise `TypeError` if `value` is not an `EncodedValue` or has the content type of a different codec."""

    if not isinstance(value, EncodedValue):
        raise TypeError(f"{codec_name} can decode only EncodedValue instances.")
    if value.content_type != expected:
        raise TypeError(f"{codec_name} cannot decode {value.content_type!r}.")


def _require_mapping_payload(codec_name: str, data: Any) -> dict[str, Any]:
    """Return the payload if it is a dictionary. Otherwise raise a `TypeError` that names the codec."""

    if not isinstance(data, dict):
        raise TypeError(f"{codec_name} payload must be a dictionary.")
    return data


def _type_key(type_: type[Any]) -> str:
    """Return the stable `module:qualname` key of a trusted Python type."""

    return f"{type_.__module__}:{type_.__qualname__}"


def _copy_json_value(value: Any) -> Any:
    """Return a copy of strict JSON-compatible data in plain lists and dictionaries.

    - **Rules:** the same rules as `JsonCodec`, with `MAX_NESTING_DEPTH`.
    - **Why:** each backend that supports JSON-shaped values can then store the
      wrapper payloads.

    Raises:
        TypeError: If the value is not JSON-compatible.
        ValueError: If lists and dictionaries nest more than
            `MAX_NESTING_DEPTH` levels deep.
    """

    error = _json_error(value)
    if error is not None:
        raise error
    return _copy_plain_json(value)


def _copy_plain_json(value: Any) -> Any:
    """Copy checked JSON data into plain lists and dictionaries.

    The recursion is safe. `_copy_json_value` calls `_json_error` first, and that
    check limits the depth.
    """

    if isinstance(value, list):
        return [_copy_plain_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _copy_plain_json(item) for key, item in value.items()}
    return deepcopy(value)


__all__ = [
    "BytesCodec",
    "DataclassCodec",
    "DatetimeCodec",
    "DecimalCodec",
    "EnumCodec",
    "UUIDCodec",
]
