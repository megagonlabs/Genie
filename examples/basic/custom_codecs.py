"""Write your own codec so that an application type survives a storage round trip.

- **Role:** a codec changes an application value into a storage-safe payload, and
  changes the payload back into the value.
- **Steps:** implement `can_encode`, `encode`, and `decode`. Give the codec a
  `content_type` that identifies its payloads. Then register the codec.
- **Order:** Genie tries codecs in registration order and uses the first codec
  whose `can_encode` returns `True`. Thus `JsonCodec` goes last, so that it
  encodes only the plain data that no other codec accepts.

Run from the repository root:

    python examples/basic/custom_codecs.py
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    CodecRegistry,
    EncodedValue,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryBlock,
    MemoryCodec,
)
from genie.codecs.stdlib import DatetimeCodec, DecimalCodec, UUIDCodec


@dataclass(frozen=True)
class Preference:
    """Custom value object that `Memory` stores through its own codec.

    Attributes:
        topic: `Preference` topic.
        value: `Preference` value.
    """

    topic: str
    value: str


@dataclass(frozen=True)
class Rating:
    """Second custom value object. The example registers its codec after it creates the registry.

    Attributes:
        subject: Thing that the rating is about.
        score: Score that the rating gives.
    """

    subject: str
    score: int


class PreferenceCodec(MemoryCodec):
    """Codec that stores `Preference` values as JSON-compatible dictionaries."""

    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        """Return `True` if this codec can encode the value.

        Args:
            value: User-level value.

        Returns:
            `True` for `Preference` instances.
        """

        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a `Preference` into a storage-safe payload.

        Args:
            value: `Preference` instance.

        Returns:
            Encoded preference payload.
        """

        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value: EncodedValue) -> Any:
        """Decode a stored preference payload.

        Args:
            value: Encoded preference payload.

        Returns:
            Decoded `Preference`.
        """

        return Preference(**value.data)


class RatingCodec(MemoryCodec):
    """Codec that stores `Rating` values as JSON-compatible dictionaries."""

    content_type = "application/vnd.genie.rating+json"

    def can_encode(self, value: Any) -> bool:
        """Return `True` if this codec can encode the value.

        Args:
            value: User-level value.

        Returns:
            `True` for `Rating` instances.
        """

        return isinstance(value, Rating)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a `Rating` into a storage-safe payload.

        Args:
            value: `Rating` instance.

        Returns:
            Encoded rating payload.
        """

        return EncodedValue(self.content_type, {"subject": value.subject, "score": value.score})

    def decode(self, value: EncodedValue) -> Any:
        """Decode a stored rating payload.

        Args:
            value: Encoded rating payload.

        Returns:
            Decoded `Rating`.
        """

        return Rating(**value.data)


def main() -> None:
    """Send custom and stdlib types through a storage round trip, and show the data that storage holds."""

    print("== Custom codecs ==")
    codecs = CodecRegistry(
        [
            DatetimeCodec(),
            DecimalCodec(),
            UUIDCodec(),
            PreferenceCodec(),
            JsonCodec(),
        ]
    )
    memory = Memory(backend=InMemoryBackend(), principal="app:demo", codecs=codecs)

    preference = memory.put(type="preference", value=Preference("tone", "warm"))
    created_at = memory.put(
        type="timestamp",
        value=dt.datetime(2026, 6, 30, 12, 15, tzinfo=dt.UTC),
    )
    budget = memory.put(type="amount", value=Decimal("19.99"))
    correlation_id = memory.put(
        type="correlation_id",
        value=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    )
    plain = memory.put(type="note", value={"body": "Plain JSON needs no custom codec"})

    decoded = require_get(memory, preference)
    print(f"decoded custom preference: {decoded.value}")
    print(f"decoded timestamp: {require_get(memory, created_at).value.isoformat()}")
    print(f"decoded amount: {require_get(memory, budget).value}")
    print(f"decoded uuid: {require_get(memory, correlation_id).value}")

    assert decoded.value == Preference("tone", "warm")
    assert require_get(memory, budget).value == Decimal("19.99")
    assert require_get(memory, plain).value == {"body": "Plain JSON needs no custom codec"}

    # Storage holds the encoded payload. Its content type records the codec that
    # made the payload. A direct backend read does not decode the payload.
    raw_preference = require_raw(memory, preference.id)
    raw_budget = require_raw(memory, budget.id)

    print(f"stored preference content type: {raw_preference.content_type}")
    print(f"stored preference payload: {raw_preference.value}")
    print(f"stored amount payload: {raw_budget.value!r}")

    assert raw_preference.content_type == PreferenceCodec.content_type
    assert raw_preference.value == {"topic": "tone", "value": "warm"}
    assert raw_budget.value == "19.99"
    # The decoded block keeps the same content type as the stored payload.
    assert decoded.content_type == PreferenceCodec.content_type

    # `Memory` raises `TypeError` for a type that no codec accepts. It does not
    # silently change the value into a string.
    try:
        memory.put(type="rating", value=Rating("retrieval", 4))
    except TypeError as error:
        print(f"storing an unregistered type: {error}")
    else:
        raise AssertionError("An unregistered value type must be refused.")

    # `CodecRegistry.register` also adds a codec after the registry exists.
    codecs.register(RatingCodec())
    rating = memory.put(type="rating", value=Rating("retrieval", 4))

    print(f"decoded rating after registering its codec: {require_get(memory, rating).value}")

    assert require_get(memory, rating).value == Rating("retrieval", 4)
    assert require_raw(memory, rating.id).content_type == RatingCodec.content_type

    # The registry keys each codec by its content type. A new codec with the same
    # content type replaces the old codec. The registry does not keep 2 codecs.
    codecs.register(RatingCodec())
    assert require_get(memory, rating).value == Rating("retrieval", 4)
    print("re-registering a content type replaced its codec")


def require_get(memory: Memory, block_id: str | MemoryBlock) -> MemoryBlock:
    """Return a decoded block, or fail with `AssertionError` if the block does not exist.

    Args:
        memory: Memory to read from.
        block_id: Block or id to read.

    Returns:
        Decoded memory block.
    """

    block = memory.get(block_id)
    assert block is not None
    return block


def require_raw(memory: Memory, block_id: str) -> MemoryBlock:
    """Return a stored block before the codecs decode it, or fail with `AssertionError` if it does not exist.

    Args:
        memory: Memory whose backend the function reads.
        block_id: Block id to read.

    Returns:
        Stored backend block.
    """

    block = memory.backend.get_block(block_id)
    assert block is not None
    return block


if __name__ == "__main__":
    main()
