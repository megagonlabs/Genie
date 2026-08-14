"""Behavioral evidence for the Mem0-inspired Genie lamp."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib

import pytest

from genie import InMemoryBackend, Memory
from genie.structures import MemoryAddress
from lamps.mem0.demo import answer_demo_question, build_demo_agent, main, teach_demo_agent
from lamps.mem0.memory_store import (
    ENTITY_LINK_RELATION,
    FACT_RECORD_TYPE,
    MENTAL_MODEL_TYPE,
    Mem0Memory,
    Mem0Structures,
    MemoryScope,
)


class SequenceClock:
    """Return predefined timestamps in order."""

    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def _store(*, clock=None):  # type: ignore[no-untyped-def]
    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="mem0-test")
    root = memory.put(type="mem0.collection", value={"name": "test"})
    handle = memory.get_handle(root=root)
    scope = MemoryScope(
        user_id="alice",
        agent_id="assistant",
        run_id="run-1",
    )
    return (
        Mem0Memory(
            handle,
            structures=Mem0Structures.create(handle, backend),
            clock=clock,
        ),
        handle,
        scope,
    )


def test_fact_record_has_oss_shaped_memory_field_and_scoped_hash_deduplication() -> None:
    store, handle, scope = _store()

    record = store.add(
        "Alice prefers window seats.",
        scope=scope,
        entities=("Alice", "flight"),
        metadata={"source": "conversation-1"},
    )
    duplicate = store.add(
        "Alice prefers window seats.",
        scope=scope,
        entities=("Alice",),
        metadata={"source": "duplicate-message"},
    )

    value = record.canonical_value()
    assert record.block.type == FACT_RECORD_TYPE
    assert set(value) == {
        "id",
        "memory",
        "user_id",
        "agent_id",
        "run_id",
        "hash",
        "metadata",
        "created_at",
        "updated_at",
    }
    assert value["id"] == record.id == record.block.id
    assert value["hash"] == hashlib.md5(value["memory"].encode("utf-8")).hexdigest()
    assert value["metadata"] == {
        "source": "conversation-1",
        "kind": "fact",
        "entities": ["Alice", "flight"],
        "occurred_at": None,
    }
    assert value["created_at"] == value["updated_at"]
    assert duplicate.id == record.id
    assert store.structures.records.get(
        MemoryAddress(
            (
                "records",
                "user=alice",
                "agent=assistant",
                "run=run-1",
            ),
            record.id,
        )
    ) == record.block
    assert len(handle.search(types=[FACT_RECORD_TYPE], limit=20)) == 1


def test_exact_scope_isolates_deduplication_and_retrieval() -> None:
    store, _, alice_scope = _store()
    bob_scope = MemoryScope(
        user_id="bob",
        agent_id="assistant",
        run_id="run-1",
    )

    alice = store.add("Prefers window seats.", scope=alice_scope, entities=("flight",))
    bob = store.add("Prefers window seats.", scope=bob_scope, entities=("flight",))

    assert alice.id != bob.id
    assert store.get(alice.id, scope=bob_scope) is None
    assert [result.memory.id for result in store.search("window", scope=alice_scope)] == [
        alice.id
    ]
    assert store.search_graph(alice.id, scope=alice_scope) == []


def test_update_preserves_identity_and_creation_time_but_changes_mutable_fields() -> None:
    created = datetime(2025, 1, 1, tzinfo=UTC)
    updated = datetime(2025, 1, 2, tzinfo=UTC)
    store, _, scope = _store(clock=SequenceClock(created, updated))
    original = store.add(
        "Alice prefers aisle seats.",
        scope=scope,
        entities=("Alice", "flight"),
        metadata={"source": "profile-v1"},
    )

    replacement = store.update(
        original.id,
        scope=scope,
        text="Alice prefers window seats.",
        metadata={"source": "conversation-2"},
    )

    assert replacement.id == original.id
    assert replacement.created_at == original.created_at == created
    assert replacement.updated_at == updated
    assert replacement.hash != original.hash
    assert replacement.text == "Alice prefers window seats."
    assert replacement.entities == original.entities
    assert replacement.metadata["source"] == "conversation-2"
    assert replacement.block.version == original.block.version + 1


def test_temporal_retrieval_uses_event_time_and_kind() -> None:
    store, _, scope = _store()
    store.add("Alice has a standing travel preference.", scope=scope)
    first = store.add(
        "The aisle booking needed correction.",
        scope=scope,
        kind="experience",
        occurred_at=datetime(2025, 4, 10, 9, tzinfo=UTC),
    )
    second = store.add(
        "The window-seat change succeeded.",
        scope=scope,
        kind="experience",
        occurred_at=datetime(2025, 4, 11, 10, tzinfo=UTC),
    )
    store.add(
        "A later trip happened outside the interval.",
        scope=scope,
        kind="experience",
        occurred_at=datetime(2025, 5, 1, tzinfo=UTC),
    )

    results = store.search_temporal(
        datetime(2025, 4, 10, tzinfo=UTC),
        datetime(2025, 4, 11, 23, 59, tzinfo=UTC),
        scope=scope,
        kinds=("experience",),
    )

    assert [record.id for record in results] == [first.id, second.id]


def test_graph_retrieval_traverses_only_shared_entity_links() -> None:
    store, handle, scope = _store()
    preference = store.add(
        "Alice prefers window seats.",
        scope=scope,
        entities=("Alice", "flight"),
    )
    experience = store.add(
        "Alice accepted the changed reservation.",
        scope=scope,
        kind="experience",
        entities=("Alice", "reservation"),
    )
    unrelated = store.add(
        "The train leaves at noon.",
        scope=scope,
        entities=("train",),
    )

    links = handle.list_links(preference.block, relation=ENTITY_LINK_RELATION, direction="both")
    results = store.search_graph(preference.id, scope=scope)

    assert len(links) == 1
    assert links[0].metadata == {"shared_entities": ["Alice"]}
    assert [(result.memory.id, result.shared_entities, result.distance) for result in results] == [
        (experience.id, ("Alice",), 1)
    ]
    assert unrelated.id not in {result.memory.id for result in results}


def test_mental_model_is_a_separate_document_with_scoped_evidence() -> None:
    store, handle, scope = _store()
    preference = store.add("Alice prefers window seats.", scope=scope)
    model = store.create_mental_model(
        name="alice-travel-profile",
        source_query="How should Alice travel?",
        content="Prefer a window seat.",
        source_memory_ids=(preference.id,),
        scope=scope,
    )

    fetched = store.get_mental_model("alice-travel-profile", scope=scope)

    assert model.block.type == MENTAL_MODEL_TYPE
    assert model.source_memory_ids == (preference.id,)
    assert fetched is not None and fetched.id == model.id
    assert len(handle.search(types=[FACT_RECORD_TYPE], limit=20)) == 1
    assert len(handle.search(types=[MENTAL_MODEL_TYPE], limit=20)) == 1


def test_agent_uses_all_four_retrieval_paths_and_exposes_a_trace(capsys) -> None:
    agent = build_demo_agent()
    teach_demo_agent(agent)

    answer = answer_demo_question(agent)

    assert "Prioritize a window seat" in answer
    assert "Alice now prefers window seats" in answer
    assert "asked me to change it" in answer
    assert "Shared-entity context" in answer
    assert [call.name for call in agent.tool_calls] == [
        "add_memory",
        "add_memory",
        "add_memory",
        "add_memory",
        "update_memory",
        "create_mental_model",
        "search_memories",
        "search_temporal",
        "search_graph",
        "get_mental_model",
    ]
    assert agent.tool_usage() == {
        "add_memory": 4,
        "update_memory": 1,
        "create_mental_model": 1,
        "search_memories": 1,
        "search_temporal": 1,
        "search_graph": 1,
        "get_mental_model": 1,
    }
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "scope",
    [
        MemoryScope(user_id="alice"),
        MemoryScope(agent_id="assistant"),
        MemoryScope(run_id="run-1"),
    ],
)
def test_each_oss_session_id_can_independently_define_a_scope(scope: MemoryScope) -> None:
    assert any(value is not None for value in scope.as_dict().values())


def test_empty_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="At least one"):
        MemoryScope()


def test_demo_prints_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("== Mem0-inspired scoped memory ==\n")
    assert "Genie structures: keyed collection, temporal collection, property graph" in output
    assert "\nTools available to the agent:\n" in output
    assert "\nAgent answer:\nQuestion:" in output
    assert "Prior experience timeline:\n- " in output
    assert "Shared-entity context:\n- " in output
    assert "Inspectable tool calls:\n01. add_memory\n02. add_memory" in output
    assert "Tool usage:\n- add_memory x4\n- update_memory x1" in output
