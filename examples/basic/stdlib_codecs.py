"""Use opt-in codecs for common Python standard-library values.

Run from the repository root:

    conda run -n "genie" python examples/basic/stdlib_codecs.py
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass
from decimal import Decimal

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory, MemoryBlock
from genie.codecs import CodecRegistry, JsonCodec
from genie.codecs.stdlib import (
    BytesCodec,
    DataclassCodec,
    DatetimeCodec,
    DecimalCodec,
    EnumCodec,
    UUIDCodec,
)


@dataclass(frozen=True)
class AuditRecord:
    """Small trusted dataclass that can be reconstructed by `DataclassCodec`."""

    request_id: str
    tags: list[str]


class Priority(str, enum.Enum):
    """Trusted enum reconstructed by `EnumCodec`."""

    HIGH = "high"
    LOW = "low"


def main() -> None:
    """Run a focused stdlib codec walkthrough."""

    print("== Standard-library codecs ==")
    section("JsonCodec stays the default")
    default_memory = Memory(backend=InMemoryBackend())
    try:
        default_memory.put(type="amount", value=Decimal("1.25"))
    except TypeError as exc:
        print(f"Decimal requires an opt-in codec: {exc}")
    else:
        raise AssertionError("Decimal should require an opt-in codec.")

    section("Register stdlib codecs explicitly")
    codecs = CodecRegistry(
        [
            BytesCodec(),
            DatetimeCodec(),
            DecimalCodec(),
            UUIDCodec(),
            DataclassCodec([AuditRecord]),
            EnumCodec([Priority]),
            JsonCodec(),
        ]
    )
    memory = Memory(backend=InMemoryBackend(), codecs=codecs)
    print("registered: bytes, datetime, decimal, UUID, dataclass, and enum codecs")

    section("Store common standard-library values")
    binary = memory.put(type="artifact", value=b"release-notes")
    timestamp = memory.put(
        type="timestamp",
        value=dt.datetime(2026, 6, 30, 12, 15, tzinfo=dt.UTC),
    )
    amount = memory.put(type="amount", value=Decimal("0.1000000000000000001"))
    identifier = memory.put(
        type="identifier",
        value=uuid.UUID("a23a8348-3b66-4ba1-bac7-d9dac418556c"),
    )
    audit = memory.put(type="audit", value=AuditRecord("req-123", ["release", "docs"]))
    priority = memory.put(type="priority", value=Priority.HIGH)

    retrieved_binary = require_get(memory, binary.id)
    retrieved_timestamp = require_get(memory, timestamp.id)
    retrieved_amount = require_get(memory, amount.id)
    retrieved_identifier = require_get(memory, identifier.id)
    retrieved_audit = require_get(memory, audit.id)
    retrieved_priority = require_get(memory, priority.id)

    print(f"bytes: {retrieved_binary.value!r}")
    print(f"timestamp: {retrieved_timestamp.value.isoformat()}")
    print(f"amount: {retrieved_amount.value}")
    print(f"uuid: {retrieved_identifier.value}")
    print(f"audit tags: {', '.join(retrieved_audit.value.tags)}")
    print(f"priority: {retrieved_priority.value.value}")

    section("Inspect storage-safe payloads")
    raw_binary = require_raw(memory, binary.id)
    raw_timestamp = require_raw(memory, timestamp.id)
    raw_amount = require_raw(memory, amount.id)
    raw_identifier = require_raw(memory, identifier.id)
    raw_audit = require_raw(memory, audit.id)
    raw_priority = require_raw(memory, priority.id)

    assert isinstance(raw_timestamp.value, dict)
    assert isinstance(raw_audit.value, dict)
    assert isinstance(raw_priority.value, dict)
    print(f"bytes content type: {raw_binary.content_type}")
    print(f"bytes payload: {raw_binary.value!r}")
    print(f"datetime payload: {raw_timestamp.value}")
    print(f"decimal payload: {raw_amount.value!r}")
    print(f"uuid payload: {raw_identifier.value!r}")
    print(f"dataclass payload keys: {sorted(raw_audit.value)}")
    print(f"enum payload: {raw_priority.value}")

    assert retrieved_binary.value == b"release-notes"
    assert retrieved_timestamp.value == timestamp.value
    assert retrieved_amount.value == Decimal("0.1000000000000000001")
    assert retrieved_identifier.value == identifier.value
    assert retrieved_audit.value == AuditRecord("req-123", ["release", "docs"])
    assert retrieved_priority.value is Priority.HIGH
    assert raw_binary.content_type == BytesCodec.content_type
    assert raw_binary.value == "cmVsZWFzZS1ub3Rlcw=="
    assert raw_timestamp.value == {
        "kind": "datetime",
        "value": "2026-06-30T12:15:00+00:00",
    }
    assert raw_amount.value == "0.1000000000000000001"
    assert raw_identifier.value == str(identifier.value)
    assert raw_audit.value["fields"] == {
        "request_id": "req-123",
        "tags": ["release", "docs"],
    }
    assert raw_priority.value["name"] == "HIGH"


def require_get(memory: Memory, block_id: str) -> MemoryBlock:
    """Return a decoded block that must exist.

    Args:
        memory: Memory handle.
        block_id: Block id to fetch.

    Returns:
        Decoded memory block.
    """

    block = memory.get(block_id)
    assert block is not None
    return block


def require_raw(memory: Memory, block_id: str) -> MemoryBlock:
    """Return a raw backend block that must exist.

    Args:
        memory: Memory handle.
        block_id: Block id to fetch.

    Returns:
        Stored backend block before codec decoding.
    """

    block = memory.backend.get_block(block_id)
    assert block is not None
    return block


def section(title: str) -> None:
    """Print a section heading.

    Args:
        title: Heading text.
    """

    print(f"\n== {title} ==")


if __name__ == "__main__":
    main()
