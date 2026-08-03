"""Behavioral evidence for the LangMem-inspired Genie lamp."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genie import InMemoryBackend, Memory
from lamps.langmem.demo import answer_demo_question, build_demo_agent, main, teach_demo_agent
from lamps.langmem.memory_store import (
    ITEM_RELATION,
    ITEM_TYPE,
    LangMemStore,
    Namespace,
    ValueSchema,
)

BASE: Namespace = ("assistant", "alice")


def _store():  # type: ignore[no-untyped-def]
    memory = Memory(backend=InMemoryBackend(), principal="langmem-test")
    root = memory.put(type="langmem.store", value={"name": "test"})
    handle = memory.get_handle(root=root)
    schemas = {
        (*BASE, "semantic"): ValueSchema(
            "semantic_fact",
            {"subject": str, "predicate": str, "object": str, "context": str},
        ),
        (*BASE, "episodes"): ValueSchema(
            "episode",
            {"observation": str, "thoughts": str, "action": str, "result": str},
        ),
        (*BASE, "mental_models"): ValueSchema(
            "mental_model",
            {"name": str, "summary": str, "principles": list, "source_keys": list},
        ),
    }
    return LangMemStore(handle, schemas=schemas), handle


def _fact(subject: str, predicate: str, object_: str, context: str) -> dict[str, str]:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object_,
        "context": context,
    }


def _episode(observation: str, result: str) -> dict[str, str]:
    return {
        "observation": observation,
        "thoughts": "Use the known preference.",
        "action": "Applied the preference.",
        "result": result,
    }


def test_items_preserve_exact_namespace_key_value_shape_across_schemas() -> None:
    store, handle = _store()
    fact = store.create(
        (*BASE, "semantic"),
        "seat",
        _fact("Alice", "prefers", "window seats", "air travel"),
    )
    episode = store.create(
        (*BASE, "episodes"),
        "booking",
        _episode("A seat needed changing.", "The change succeeded."),
    )

    assert fact.block.type == episode.block.type == ITEM_TYPE
    assert set(fact.canonical_value()) == {"namespace", "key", "value"}
    assert fact.canonical_value() == {
        "namespace": ["assistant", "alice", "semantic"],
        "key": "seat",
        "value": _fact("Alice", "prefers", "window seats", "air travel"),
    }
    assert episode.canonical_value()["value"] == _episode(
        "A seat needed changing.", "The change succeeded."
    )
    assert fact.schema_name == "semantic_fact"
    assert episode.schema_name == "episode"
    assert len(handle.search(types=[ITEM_TYPE], limit=20)) == 2


def test_supplied_schema_rejects_missing_extra_and_wrong_type_fields() -> None:
    store, _ = _store()
    namespace = (*BASE, "semantic")

    with pytest.raises(ValueError, match="missing fields"):
        store.create(namespace, "missing", {"subject": "Alice"})
    with pytest.raises(ValueError, match="does not define"):
        store.create(
            namespace,
            "extra",
            {**_fact("Alice", "prefers", "window", "travel"), "confidence": 1.0},
        )
    with pytest.raises(TypeError, match="must be str"):
        store.create(
            namespace,
            "wrong-type",
            {**_fact("Alice", "prefers", "window", "travel"), "object": ["window"]},
        )


def test_unregistered_namespace_accepts_a_different_value_shape() -> None:
    store, _ = _store()

    item = store.create((*BASE, "freeform"), "note", "Remember the confirmation code.")

    assert item.value == "Remember the confirmation code."
    assert item.schema_name is None


def test_contextual_key_is_unique_only_inside_its_exact_namespace() -> None:
    store, _ = _store()
    semantic = store.create(
        (*BASE, "semantic"),
        "current",
        _fact("Alice", "prefers", "window", "travel"),
    )
    episode = store.create(
        (*BASE, "episodes"),
        "current",
        _episode("A booking changed.", "The user accepted it."),
    )

    assert semantic.block.id != episode.block.id
    assert store.get((*BASE, "semantic"), "current") == semantic
    assert store.get((*BASE, "episodes"), "current") == episode
    with pytest.raises(ValueError, match="already exists"):
        store.create(
            (*BASE, "semantic"),
            "current",
            _fact("Alice", "prefers", "aisle", "travel"),
        )


def test_update_preserves_item_identity_namespace_and_key() -> None:
    store, _ = _store()
    original = store.create(
        (*BASE, "semantic"),
        "seat",
        _fact("Alice", "prefers", "aisle", "air travel"),
        entities=("Alice", "flight"),
    )

    updated = store.update(
        (*BASE, "semantic"),
        "seat",
        _fact("Alice", "prefers", "window", "air travel"),
    )

    assert updated.block.id == original.block.id
    assert updated.namespace == original.namespace
    assert updated.key == original.key
    assert updated.value["object"] == "window"
    assert updated.entities == original.entities
    assert updated.block.version == original.block.version + 1


def test_lexical_search_respects_namespace_prefix_and_collection_filter() -> None:
    store, _ = _store()
    fact = store.create(
        (*BASE, "semantic"),
        "seat",
        _fact("Alice", "prefers", "window seats", "air travel"),
    )
    store.create(
        (*BASE, "episodes"),
        "seat-change",
        _episode("Alice requested a window seat.", "The flight booking changed."),
    )
    store.create(("assistant", "bob", "freeform"), "seat", "Bob prefers a window seat.")

    results = store.search(BASE, "Alice window travel", collections=("semantic",))

    assert [result.item for result in results] == [fact]
    assert results[0].score > 0


def test_temporal_retrieval_uses_annotations_without_changing_episode_value() -> None:
    store, _ = _store()
    first = store.create(
        (*BASE, "episodes"),
        "first",
        _episode("The aisle seat needed changing.", "A window seat was found."),
        occurred_at=datetime(2025, 4, 10, 9, tzinfo=UTC),
    )
    second = store.create(
        (*BASE, "episodes"),
        "second",
        _episode("The new seat was ready.", "Alice accepted it."),
        occurred_at=datetime(2025, 4, 11, 10, tzinfo=UTC),
    )
    store.create(
        (*BASE, "episodes"),
        "later",
        _episode("Another trip started.", "It completed."),
        occurred_at=datetime(2025, 5, 1, tzinfo=UTC),
    )

    results = store.search_temporal(
        BASE,
        datetime(2025, 4, 10, tzinfo=UTC),
        datetime(2025, 4, 11, 23, 59, tzinfo=UTC),
        collections=("episodes",),
    )

    assert [item.key for item in results] == [first.key, second.key]
    assert set(first.value) == {"observation", "thoughts", "action", "result"}
    assert "occurred_at" not in first.canonical_value()["value"]


def test_graph_retrieval_traverses_typed_links_inside_namespace_scope() -> None:
    store, handle = _store()
    fact = store.create(
        (*BASE, "semantic"),
        "seat",
        _fact("Alice", "prefers", "window", "travel"),
    )
    episode = store.create(
        (*BASE, "episodes"),
        "fix",
        _episode("A seat needed changing.", "The correction succeeded."),
    )
    model = store.create(
        (*BASE, "mental_models"),
        "travel",
        {
            "name": "Travel profile",
            "summary": "Use window seats.",
            "principles": ["Check preferences."],
            "source_keys": ["semantic/seat", "episodes/fix"],
        },
    )
    store.link(fact.namespace, fact.key, episode.namespace, episode.key, relation="demonstrated_by")
    store.link(episode.namespace, episode.key, model.namespace, model.key, relation="supports")

    results = store.search_graph(BASE, fact.namespace, fact.key, max_depth=2)
    links = handle.list_links(fact.block, relation=ITEM_RELATION, direction="out")

    assert [(result.item.key, result.relation, result.distance) for result in results] == [
        ("fix", "demonstrated_by", 1),
        ("travel", "supports", 2),
    ]
    assert len(links) == 1


def test_mental_model_is_a_regular_item_with_its_own_supplied_schema() -> None:
    store, _ = _store()
    model = store.create(
        (*BASE, "mental_models"),
        "travel",
        {
            "name": "Travel profile",
            "summary": "Use window seats.",
            "principles": ["Check preferences."],
            "source_keys": ["semantic/seat"],
        },
    )

    assert model.schema_name == "mental_model"
    assert model.namespace[-1] == "mental_models"
    assert store.get(model.namespace, model.key) == model


def test_agent_uses_tools_for_writes_and_all_four_retrieval_modes(capsys) -> None:
    agent = build_demo_agent()
    teach_demo_agent(agent)

    answer = answer_demo_question(agent)

    assert "Prioritize window seats" in answer
    assert "Alice prefers Riverside Inn" in answer
    assert "Alice accepted the updated reservation" in answer
    assert "demonstrated_by" in answer
    assert [call.name for call in agent.call_log] == [
        "manage_memory",
        "manage_memory",
        "manage_memory",
        "manage_memory",
        "manage_memory",
        "link_memories",
        "link_memories",
        "link_memories",
        "get_memory",
        "search_memory",
        "search_temporal",
        "search_graph",
    ]
    assert agent.tool_usage() == {
        "manage_memory": 5,
        "link_memories": 3,
        "get_memory": 1,
        "search_memory": 1,
        "search_temporal": 1,
        "search_graph": 1,
    }
    assert capsys.readouterr().out == ""


def test_demo_uses_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("Tools available to the agent:\n")
    assert "\nAgent answer:\nQuestion:" in output
    assert "Inspectable tool calls:\n01. manage_memory\n02. manage_memory" in output
    assert "Tool usage:\n- manage_memory x5\n- link_memories x3" in output
