# Codecs

`genie.codecs` converts application values to storage-safe data and restores
them on read. Each encoded value contains a codec `content_type` and the data
stored in `MemoryBlock.value`.

## Default JSON codec

`Memory` registers only `JsonCodec` by default. It accepts `None`, strings,
booleans, integers, finite floats, lists, and dictionaries with string keys.
Encoding and decoding deep-copy values; `NaN` and infinity are rejected.

## Selection

`CodecRegistry` uses an explicitly requested `content_type`, or the first codec
whose `can_encode` accepts the value. The stored content type selects the codec
again during decoding.

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

Choose stable content types and portable encoded data. Backends should not need
application knowledge to interpret the original Python object.

## Opt-in standard-library codecs

- `BytesCodec`
- `DatetimeCodec`
- `DecimalCodec`
- `UUIDCodec`
- `DataclassCodec`
- `EnumCodec`

Register only required types. Keep domain taxonomies and validation outside
Genie codecs.

Related: [Memory](../memory/README.md) and [Backends](../backend/README.md).
