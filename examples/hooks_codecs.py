"""Use hooks for lifecycle behavior and codecs for custom value types.

Run from the repository root:

    conda run -n "genie" python examples/hooks_codecs.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import CodecRegistry, EncodedValue, JsonCodec, Memory, MemoryCodec, MemoryHook


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

    def before_put(self, block):
        """Mark blocks before they are stored."""

        return block.with_updates(metadata={**block.metadata, "audited": True})

    def after_put(self, block) -> None:
        """Record stored block ids."""

        self.created.append(block.id)


def main() -> None:
    """Run a focused hooks and codecs walkthrough."""

    audit = AuditHook()
    memory = Memory(
        hooks=[audit],
        codecs=CodecRegistry([JsonCodec(), PreferenceCodec()]),
    )

    preference = memory.put(type="preference", value=Preference("tone", "warm"))
    retrieved = memory.get(preference)

    assert retrieved is not None
    print(f"decoded preference: {retrieved.value}")
    print(f"audit events: {len(audit.created)}")

    assert retrieved.value == Preference("tone", "warm")
    assert retrieved.metadata["audited"] is True
    assert audit.created == [preference.id]


if __name__ == "__main__":
    main()
