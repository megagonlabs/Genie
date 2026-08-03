"""Behavioral evidence for the MemoryOS-inspired Genie lamp."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genie import InMemoryBackend, Memory
from lamps.memoryos.demo import answer_demo_question, build_demo_agent, main, teach_demo_agent
from lamps.memoryos.memory_system import (
    KNOWLEDGE_TYPE,
    MID_TERM_TYPE,
    NEXT_PAGE_RELATION,
    PROFILE_TYPE,
    PROMOTED_TO_RELATION,
    SHORT_TERM_TYPE,
    KnowledgeDraft,
    MemoryOSMemory,
)


def _memory(
    *,
    short_capacity: int = 2,
    heat_threshold: float = 2.0,
    knowledge_capacity: int = 10,
    clock=None,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    memory = Memory(backend=InMemoryBackend(), principal="memoryos-test")
    root = memory.put(type="memoryos.root", value={"user": "alice"})
    handle = memory.get_handle(root=root)
    hierarchy = MemoryOSMemory(
        handle,
        short_term_capacity=short_capacity,
        heat_threshold=heat_threshold,
        knowledge_capacity=knowledge_capacity,
        clock=clock,
    )
    return hierarchy, handle


def _add(
    memory: MemoryOSMemory,
    key: str,
    day: int,
    *,
    summary: str | None = None,
):  # type: ignore[no-untyped-def]
    return memory.add_memory(
        key=key,
        user_input=f"User message about {key}.",
        agent_response=f"Agent response about {key}.",
        occurred_at=datetime(2025, 4, day, 9, tzinfo=UTC),
        summary=summary or f"Summary about {key}.",
        entities=("Alice",),
    )


def test_short_term_is_fifo_and_overflow_becomes_a_mid_term_page() -> None:
    memory, handle = _memory(short_capacity=2)
    _add(memory, "first", 1)
    _add(memory, "second", 2)
    result = _add(memory, "third", 3)

    short = memory.short_term()
    pages = memory.mid_term()

    assert [qa.key for qa in short] == ["second", "third"]
    assert [page.source_key for page in pages] == ["first"]
    assert [page.source_key for page in result.promoted_pages] == ["first"]
    assert set(short[0].block.value) == {"key", "user_input", "agent_response", "timestamp"}
    assert len(handle.search(types=[SHORT_TERM_TYPE], limit=20)) == 2
    assert len(handle.search(types=[MID_TERM_TYPE], limit=20)) == 1


def test_mid_term_pages_store_summary_dialogue_heat_and_chronology_links() -> None:
    memory, handle = _memory(short_capacity=1)
    _add(memory, "first", 1, summary="First summarized dialogue.")
    _add(memory, "second", 2, summary="Second summarized dialogue.")
    _add(memory, "third", 3)

    first, second = memory.mid_term()
    links = handle.list_links(first.block, relation=NEXT_PAGE_RELATION, direction="out")

    assert first.summary == "First summarized dialogue."
    assert first.heat == 1.0
    assert first.visit_count == 0
    assert set(first.block.value) == {
        "page_id",
        "source_qa_key",
        "summary",
        "dialogue",
        "heat",
        "visit_count",
    }
    assert len(links) == 1 and links[0].target_id == second.id


def test_matching_retrieval_increments_page_heat_and_visit_count() -> None:
    memory, _ = _memory(short_capacity=1)
    _add(memory, "window-seat", 1, summary="Alice prefers a window seat.")
    _add(memory, "current", 2)

    before = memory.mid_term()[0]
    bundle = memory.retrieve_memory("Alice window seat")
    after = memory.mid_term()[0]

    assert before.heat == 1.0 and before.visit_count == 0
    assert [page.id for page in bundle.mid_term] == [before.id]
    assert after.heat == 2.0 and after.visit_count == 1


def test_long_term_update_requires_heated_source_pages() -> None:
    memory, _ = _memory(short_capacity=1, heat_threshold=2.0)
    _add(memory, "window-seat", 1, summary="Alice prefers a window seat.")
    _add(memory, "current", 2)
    page = memory.mid_term()[0]

    with pytest.raises(ValueError, match="below threshold"):
        memory.update_long_term(
            profile_updates={"seat": "window"},
            profile_source_page_ids=(page.id,),
        )

    memory.retrieve_memory("Alice window seat")
    profile, _, _ = memory.update_long_term(
        profile_updates={"seat": "window"},
        profile_source_page_ids=(page.id,),
    )
    assert profile.data == {"seat": "window"}


def test_long_term_profile_and_both_knowledge_collections_keep_sources() -> None:
    memory, handle = _memory(short_capacity=1)
    _add(memory, "preference", 1, summary="Alice prefers window seats.")
    _add(memory, "current", 2)
    page = memory.mid_term()[0]
    memory.retrieve_memory("Alice window seats")

    profile, user_entries, assistant_entries = memory.update_long_term(
        profile_updates={"seat_preference": "window"},
        profile_source_page_ids=(page.id,),
        user_knowledge=(
            KnowledgeDraft("Alice prefers window seats.", (page.id,), ("Alice",)),
        ),
        assistant_knowledge=(
            KnowledgeDraft("Check seat preference before booking.", (page.id,), ("flight",)),
        ),
    )

    assert profile.block.type == PROFILE_TYPE
    assert user_entries[0].owner == "user"
    assert assistant_entries[0].owner == "assistant"
    assert user_entries[0].source_page_ids == (page.id,)
    assert len(handle.search(types=[KNOWLEDGE_TYPE], limit=20)) == 2
    links = handle.list_links(page.block, relation=PROMOTED_TO_RELATION, direction="out")
    assert {link.target_id for link in links} == {
        profile.block.id,
        user_entries[0].block.id,
        assistant_entries[0].block.id,
    }


def test_retrieval_combines_every_memory_tier() -> None:
    memory, _ = _memory(short_capacity=1)
    _add(memory, "preference", 1, summary="Alice prefers window seats.")
    _add(memory, "current", 2, summary="Current travel request.")
    page = memory.mid_term()[0]
    memory.retrieve_memory("Alice window travel")
    memory.update_long_term(
        profile_updates={"seat_preference": "window"},
        profile_source_page_ids=(page.id,),
        user_knowledge=(KnowledgeDraft("Alice prefers window seats.", (page.id,)),),
        assistant_knowledge=(
            KnowledgeDraft("Use known travel preferences.", (page.id,)),
        ),
    )

    bundle = memory.retrieve_memory("Alice window travel preferences")

    assert [qa.key for qa in bundle.short_term] == ["current"]
    assert [item.source_key for item in bundle.mid_term] == ["preference"]
    assert bundle.user_profile is not None
    assert [entry.owner for entry in bundle.user_knowledge] == ["user"]
    assert [entry.owner for entry in bundle.assistant_knowledge] == ["assistant"]


def test_temporal_retrieval_orders_short_mid_and_long_term_entries() -> None:
    clock = lambda: datetime(2025, 4, 3, 12, tzinfo=UTC)
    memory, _ = _memory(short_capacity=1, clock=clock)
    _add(memory, "first", 1, summary="First experience.")
    _add(memory, "second", 2, summary="Second experience.")
    page = memory.mid_term()[0]
    memory.retrieve_memory("First experience")
    memory.update_long_term(
        profile_updates={"preference": "known"},
        profile_source_page_ids=(page.id,),
        user_knowledge=(KnowledgeDraft("A dated fact.", (page.id,)),),
    )

    results = memory.retrieve_temporal(
        datetime(2025, 4, 1, tzinfo=UTC),
        datetime(2025, 4, 3, 23, tzinfo=UTC),
    )

    assert [result.layer for result in results] == [
        "mid_term",
        "short_term",
        "user_knowledge",
    ]


def test_graph_retrieval_combines_page_chain_and_promotion_links() -> None:
    memory, _ = _memory(short_capacity=1)
    _add(memory, "first", 1, summary="Alice window preference.")
    _add(memory, "second", 2, summary="A booking correction.")
    _add(memory, "third", 3)
    first, second = memory.mid_term()
    memory.retrieve_memory("Alice window booking correction")
    profile, user_entries, _ = memory.update_long_term(
        profile_updates={"seat": "window"},
        profile_source_page_ids=(first.id,),
        user_knowledge=(KnowledgeDraft("Alice likes window seats.", (first.id,)),),
    )

    results = memory.retrieve_graph(first.id, max_depth=1)

    assert {(result.relation, result.block.id) for result in results} == {
        (NEXT_PAGE_RELATION, second.id),
        (PROMOTED_TO_RELATION, profile.block.id),
        (PROMOTED_TO_RELATION, user_entries[0].block.id),
    }


def test_long_term_knowledge_collections_are_bounded_fifo() -> None:
    memory, _ = _memory(short_capacity=1, knowledge_capacity=1)
    _add(memory, "source", 1, summary="A useful source memory.")
    _add(memory, "current", 2)
    page = memory.mid_term()[0]
    memory.retrieve_memory("useful source memory")
    memory.update_long_term(
        profile_updates={"ready": True},
        profile_source_page_ids=(page.id,),
        user_knowledge=(
            KnowledgeDraft("First fact.", (page.id,)),
            KnowledgeDraft("Second fact.", (page.id,)),
        ),
    )

    assert [entry.knowledge for entry in memory.knowledge("user")] == ["Second fact."]


def test_agent_exercises_layering_and_all_retrieval_paths(capsys) -> None:
    agent = build_demo_agent()
    teach_demo_agent(agent)

    answer = answer_demo_question(agent)

    assert "seat=window" in answer
    assert "Alice prefers Riverside Inn" in answer
    assert "A prior aisle-seat booking required a correction" in answer
    assert "memoryos.next_page" in answer
    assert [call.name for call in agent.call_log] == [
        "add_memory",
        "add_memory",
        "add_memory",
        "add_memory",
        "add_memory",
        "retrieve_memory",
        "update_long_term",
        "get_user_profile",
        "retrieve_memory",
        "retrieve_temporal",
        "retrieve_graph",
    ]
    assert agent.tool_usage() == {
        "add_memory": 5,
        "retrieve_memory": 2,
        "update_long_term": 1,
        "get_user_profile": 1,
        "retrieve_temporal": 1,
        "retrieve_graph": 1,
    }
    assert capsys.readouterr().out == ""


def test_demo_uses_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("Tools available to the agent:\n")
    assert "\nAgent answer:\nQuestion:" in output
    assert "Inspectable tool calls:\n01. add_memory\n02. add_memory" in output
    assert "Tool usage:\n- add_memory x5\n- retrieve_memory x2" in output
