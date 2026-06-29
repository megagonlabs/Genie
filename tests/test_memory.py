from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    CodecRegistry,
    EncodedValue,
    InMemoryBackend,
    JsonCodec,
    Memory,
    MemoryCodec,
    MemoryHook,
    MemoryPolicy,
)


def test_put_get_and_patch_memory_block() -> None:
    memory = Memory()

    block = memory.put(type="preference", value={"style": "concise"})
    updated = memory.patch(block, value={"tone": "warm"}, metadata={"source": "test"})

    assert updated.value == {"style": "concise", "tone": "warm"}
    assert updated.metadata["source"] == "test"
    assert updated.version == 2
    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value["tone"] == "warm"


def test_subtree_scope_limits_search_scope() -> None:
    memory = Memory()
    root = memory.put(type="root", value="workspace")
    task = memory.put(parent=root, type="task", value="launch task")
    memory.put(parent=task, type="note", value="subtree only clue")
    memory.put(type="note", value="global only clue")

    scope = memory.scope(root=task)

    assert [result.block.value for result in scope.search("clue")] == ["subtree only clue"]
    with pytest.raises(AccessDenied):
        scope.search("clue", scope="global")


def test_scope_can_optionally_search_ancestors() -> None:
    memory = Memory()
    root = memory.put(type="root", value="project launch context")
    task = memory.put(parent=root, type="task", value="implementation task")

    restricted = memory.scope(root=task)
    with pytest.raises(AccessDenied):
        restricted.search("project", scope="ancestors")

    allowed = memory.scope(root=task, allow_up=True)
    results = allowed.search("project", scope="ancestors")

    assert [result.block.id for result in results] == [root.id]


def test_block_policy_denies_read_to_other_principal() -> None:
    owner_memory = Memory(principal="owner")
    private_policy = MemoryPolicy(read=AccessRule.only(principals={"owner"}))
    private = owner_memory.put(type="secret", value="hidden", policy=private_policy)

    other_memory = Memory(
        backend=owner_memory.backend,
        principal="other",
        policy_engine=owner_memory.policy_engine,
        hooks=owner_memory.hooks,
        codecs=owner_memory.codecs,
    )

    with pytest.raises(AccessDenied):
        other_memory.get(private.id)


def test_hooks_can_transform_and_observe_blocks() -> None:
    class SourceHook(MemoryHook):
        def __init__(self) -> None:
            self.created: list[str] = []

        def before_put(self, block):
            return block.with_updates(metadata={**block.metadata, "hooked": True})

        def after_put(self, block):
            self.created.append(block.id)

    hook = SourceHook()
    memory = Memory(hooks=[hook])

    block = memory.put(type="note", value="hello")

    assert block.metadata["hooked"] is True
    assert hook.created == [block.id]


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


def test_custom_codec_round_trips_user_types() -> None:
    codecs = CodecRegistry([JsonCodec(), PreferenceCodec()])
    memory = Memory(codecs=codecs)

    block = memory.put(type="preference", value=Preference("style", "concise"))

    assert block.value == Preference("style", "concise")
    retrieved = memory.get(block.id)
    assert retrieved is not None
    assert retrieved.value == Preference("style", "concise")


def test_backends_can_be_shared_between_memory_instances() -> None:
    backend = InMemoryBackend()
    writer = Memory(backend=backend, principal="writer")
    reader = Memory(backend=backend, principal="reader")

    block = writer.put(type="note", value="shared backend")

    retrieved = reader.get(block.id)
    assert retrieved is not None
    assert retrieved.value == "shared backend"
