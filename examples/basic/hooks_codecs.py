"""Use hooks around memory operations and codecs for custom and stdlib values.

Run from the repository root:

    conda run -n "genie" python examples/basic/hooks_codecs.py
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
from typing import Any
import uuid

if __package__ in {None, ""}:
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    CodecRegistry,
    EncodedValue,
    HookContext,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryBlock,
    MemoryCodec,
    MemoryHook,
)
from genie.codecs.stdlib import DatetimeCodec, DecimalCodec, UUIDCodec


@dataclass(frozen=True)
class Preference:
    """Custom value object used to demonstrate codecs.

    Attributes:
        topic: Preference topic.
        value: Preference value.
    """

    topic: str
    value: str


class PreferenceCodec(MemoryCodec):
    """Codec that stores `Preference` values as JSON-compatible dictionaries."""

    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether this codec can encode the value.

        Args:
            value: User-level value.

        Returns:
            `True` for `Preference` instances.
        """

        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a `Preference` into a storage-safe payload.

        Args:
            value: Preference instance.

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


class AuditHook(MemoryHook):
    """Hook that marks created blocks and records their ids."""

    def __init__(self) -> None:
        """Create an empty audit hook."""

        self.created: list[str] = []

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Mark blocks before they are stored."""

        return block.with_updates(metadata={**block.metadata, "audited": True})

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record stored block ids."""

        self.created.append(block.id)


def main() -> None:
    """Run a focused hooks and codecs walkthrough."""

    print("== Hooks and codecs ==")
    audit = AuditHook()
    memory = Memory(
        backend=InMemoryBackend(),
        hooks=[audit],
        codecs=CodecRegistry(
            [
                DatetimeCodec(),
                DecimalCodec(),
                UUIDCodec(),
                PreferenceCodec(),
                JsonCodec(),
            ]
        ),
    )

    preference = memory.put(type="preference", value=Preference("tone", "warm"))
    created_at = memory.put(
        type="timestamp",
        value=dt.datetime(2026, 6, 30, 12, 15, tzinfo=dt.timezone.utc),
    )
    budget = memory.put(type="amount", value=Decimal("19.99"))
    correlation_id = memory.put(
        type="correlation_id",
        value=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    )
    retrieved = memory.get(preference)
    retrieved_created_at = memory.get(created_at)
    retrieved_budget = memory.get(budget)
    retrieved_correlation_id = memory.get(correlation_id)
    raw_budget = memory.backend.get_block(budget.id)

    assert retrieved is not None
    assert retrieved_created_at is not None
    assert retrieved_budget is not None
    assert retrieved_correlation_id is not None
    assert raw_budget is not None
    print(f"decoded custom preference: {retrieved.value}")
    print(f"decoded timestamp: {retrieved_created_at.value.isoformat()}")
    print(f"decoded amount: {retrieved_budget.value}")
    print(f"decoded uuid: {retrieved_correlation_id.value}")
    print(f"raw storage payload for amount: {raw_budget.value!r}")
    print(f"blocks observed by the audit hook: {len(audit.created)}")

    assert retrieved.value == Preference("tone", "warm")
    assert retrieved_created_at.value == dt.datetime(
        2026,
        6,
        30,
        12,
        15,
        tzinfo=dt.timezone.utc,
    )
    assert retrieved_budget.value == Decimal("19.99")
    assert retrieved_correlation_id.value == uuid.UUID(
        "22222222-2222-4222-8222-222222222222"
    )
    assert raw_budget.value == "19.99"
    assert retrieved.metadata["audited"] is True
    assert audit.created == [
        preference.id,
        created_at.id,
        budget.id,
        correlation_id.id,
    ]


if __name__ == "__main__":
    main()
