# Codecs

`genie.codecs` changes application values into data that is safe to store. On
read, it changes that data back into the values.

- **`EncodedValue`:** holds the codec `content_type` and the encoded `data`.
- **Storage:** `Memory` keeps `data` in `MemoryBlock.value` and the codec `content_type` in `MemoryBlock.content_type`.

## Default JSON codec

`Memory` registers only `JsonCodec` by default.

- **Accepts** `None`, strings, booleans, integers, finite floats, lists, and dictionaries whose keys are strings.
- **Rejects** `NaN`, infinity, and `enum.Enum` members. This includes members of an enum that is also a `str` or an `int`.
- **Limits depth** to `MAX_NESTING_DEPTH` (100) levels of lists and dictionaries. A deeper value, or a value that contains itself, raises `ValueError` on encode and `TypeError` on decode.
- **Deep-copies** the value on encode and on decode.

## Selection

- **With a `content_type`:** the registry uses exactly that codec.
- **Without one:** the registry uses the first codec, in registration order, whose `can_encode` accepts the value. A replacement codec keeps the position of the codec that it replaces.
- **A bad value of a known kind still selects its codec.** `JsonCodec.can_encode` returns `True` for a value that is too deep. So `encode` raises `ValueError`, and the registry does not report "no codec".
- **On decode:** the stored content type selects the codec again.
- **Each `Memory` has its own registry.** Two `Memory` objects over one backend can have different codecs.
- **`search` decodes only visible results.** It decodes a result only after the policy and scope filters keep it. So a hidden block with an unknown content type does not change the results.
- **`delete` decodes the whole subtree.** The delete hooks get decoded blocks. So a missing codec raises `TypeError`, and the backend deletes nothing.
- **Rules:** a content type is a non-empty string. `encode` returns an `EncodedValue` with the content type of its own codec. If a codec breaks a rule, the registry raises `TypeError` or `ValueError`.

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

- **Stable content types:** decode finds the codec from the content type that each block stores. If a codec changes its content type, the registry cannot decode the blocks that have the old content type.
- **Portable encoded data:** a backend reads the stored data without knowledge of the application.
- **`JsonCodec` checks stored payloads again on decode.** So bad backend data raises an error and does not get to application code.
- **Genie does not limit the depth of `EncodedValue.data` from a custom codec.** Adapter JSON checks and copies recurse once per level. So very deep data from a custom codec can raise `RecursionError` in the backend.

## Opt-in standard-library codecs

`BytesCodec`, `DatetimeCodec`, `DecimalCodec`, `UUIDCodec`, `DataclassCodec`,
and `EnumCodec`.

- **Each codec is opt-in.** An application registers only the codecs and the types that it uses.
- **Domain taxonomies and validation stay outside Genie codecs.**

Related: [Memory](../memory/README.md) and [Backends](../backend/README.md).
