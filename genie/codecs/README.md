# Codecs

The `genie.codecs` package converts application values to storage-safe data
and reconstructs them when blocks are read.

Each encoded value contains:

- a `content_type` that identifies the codec; and
- the encoded data stored in `MemoryBlock.value`.

## Default JSON codec

`Memory()` registers only `JsonCodec` by default. It supports:

- `None`, strings, booleans, and integers;
- finite floats;
- lists; and
- dictionaries with string keys.

The codec deep-copies values during encode and decode. A caller therefore
cannot change stored state by mutating a returned list or dictionary. `NaN`
and infinity are rejected because they are not strict JSON values.

## Selecting a codec

`CodecRegistry` selects a codec in one of two ways:

1. `content_type=` requests an exact registered codec.
2. Without a content type, the registry uses the first codec whose
   `can_encode` method accepts the value.

The stored content type is used again during decoding.

```python
memory.codecs.register(PreferenceCodec())
block = memory.put(type="preference", value=preference)
```

## Custom codecs

A custom codec implements `content_type`, `can_encode`, `encode`, and `decode`:

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

Choose stable content types and portable payloads. A backend stores the
encoded representation; it should not need application-specific knowledge to
interpret the original Python object.

## Standard-library codecs

Dependency-free codecs are available but opt-in:

- `BytesCodec`
- `DatetimeCodec`
- `DecimalCodec`
- `UUIDCodec`
- `DataclassCodec`
- `EnumCodec`

Register only the types an application needs. Keep application taxonomies and
domain validation outside Genie codecs.

## Related guides

- [Memory blocks](../memory/README.md)
- [Backends](../backend/README.md)
