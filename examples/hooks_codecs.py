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
    topic: str
    value: str


class PreferenceCodec(MemoryCodec):
    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        return EncodedValue(
            self.content_type,
            {"topic": value.topic, "value": value.value},
        )

    def decode(self, value: EncodedValue) -> Any:
        return Preference(**value.data)


class AuditHook(MemoryHook):
    def __init__(self) -> None:
        self.created: list[str] = []

    def before_put(self, block):
        return block.with_updates(metadata={**block.metadata, "audited": True})

    def after_put(self, block) -> None:
        self.created.append(block.id)


def main() -> None:
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
