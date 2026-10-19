# Codecs

`genie.codecs` turns application values into data that is safe to store, and
rebuilds them on read. Each encoded value carries a codec `content_type` and the
data that is stored in `MemoryBlock.value`.

## Default JSON codec

`Memory` registers only `JsonCodec` by default.

- **Accepts** `None`, strings, booleans, integers, finite floats, lists, and dictionaries whose keys are strings.
- **Rejects** `NaN` and infinity.
- **Limits nesting** to `MAX_NESTING_DEPTH` (100) levels of lists and dictionaries. Deeper values, and values that contain themselves, raise `ValueError`.
- **Deep-copies** on both encode and decode.

## Selection

- **With a `content_type`:** the registry uses exactly that codec.
- **Without one:** it uses the first codec whose `can_encode` accepts the value.
- **A bad value of a known kind still selects its codec.** `JsonCodec.can_encode` returns `True` for a value that is too deep, so `encode` raises `ValueError` and the registry does not report "no codec".
- **On decode:** the stored content type picks the codec again.
- **Rules:** a content type must be a non-empty string, and `encode` must return an `EncodedValue` that carries the codec's own content type.

```python
memory.codecs.register(PreferenceCodec())
block = memory.put(type="preference", value=preference)
```

## Custom codecs

```python
class PreferenceCodec:
    content_type = "application/vnd.example.preference+json"

    def can_encode(self, value):
        return isinstance(value, Preference)

    def encode(self, value):
        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value):
        return Preference(**value.data)
```

- **Choose stable content types and portable encoded data.** A backend should not need to know anything about your application to read the stored data.
- **`JsonCodec` re-checks stored payloads while decoding,** so bad backend data fails instead of reaching application code.
- **Genie does not limit the depth of a custom codec's `EncodedValue.data`.** Adapter JSON checks recurse once per level, so very deep data from a custom codec can raise `RecursionError` in the backend.

## Opt-in standard-library codecs

`BytesCodec`, `DatetimeCodec`, `DecimalCodec`, `UUIDCodec`, `DataclassCodec`,
and `EnumCodec`.

- **Register only the types you need.**
- **Keep domain taxonomies and validation outside Genie codecs.**

Related: [Memory](../memory/README.md) and [Backends](../backend/README.md).
