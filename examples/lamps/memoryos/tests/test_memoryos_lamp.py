"""Behavioral evidence for the MemoryOS-inspired Genie lamp."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from examples.lamps.memoryos.agent import MemoryOSAgent, MemoryOSTools
from examples.lamps.memoryos.demo import build_agent, main, report, run_conversation
from examples.lamps.memoryos.long_term import LongTermMemory
from examples.lamps.memoryos.memoryos import MemoryOS
from examples.lamps.memoryos.mid_term import MidTermMemory
from examples.lamps.memoryos.model import (
    ASSISTANT,
    KNOWLEDGE_TYPE,
    PAGE_NAMESPACE,
    PAGE_TYPE,
    PROFILE_TYPE,
    QA_TYPE,
    REVISION_TYPE,
    SEGMENT_NAMESPACE,
    SEGMENT_TYPE,
    SYSTEM_TYPE,
    USER,
    Exchange,
    Insight,
    segment_heat,
    time_decay,
)
from examples.lamps.memoryos.retriever import Retriever
from examples.lamps.memoryos.short_term import ShortTermMemory
from examples.lamps.memoryos.structures import MemoryOSStructures
from examples.lamps.memoryos.updater import Updater
from genie import InMemoryBackend, Memory, MemoryConflict, MemoryHandle, Principal
from genie.structures import MemoryAddress

START = datetime(2025, 4, 8, 9, tzinfo=UTC)

SEATS = "flight seat preferences"
HOTEL = "Austin hotel choices"
MEALS = "meal requirements"


@dataclass(frozen=True, slots=True)
class Lamp:
    """The wired pieces one test works against."""

    handle: MemoryHandle
    structures: MemoryOSStructures
    short_term: ShortTermMemory
    mid_term: MidTermMemory
    long_term: LongTermMemory
    updater: Updater
    system: MemoryOS


def _lamp(
    *,
    capacity: int = 3,
    mid_capacity: int = 10,
    knowledge_capacity: int = 10,
    threshold: float = 5.0,
    queue_capacity: int = 7,
    shared: tuple[Memory, InMemoryBackend] | None = None,
    user_id: str = "alice",
) -> Lamp:
    """Build an empty hierarchy, optionally beside another in one store."""

    store, backend = shared if shared is not None else _store()
    root = store.put(type=SYSTEM_TYPE, value={"user_id": user_id, "assistant_id": "travel-agent"})
    handle = store.get_handle(root=root)
    structures = MemoryOSStructures.create(handle, backend)
    short_term = ShortTermMemory(handle, structures=structures, capacity=capacity)
    mid_term = MidTermMemory(handle, structures=structures, capacity=mid_capacity)
    long_term = LongTermMemory(handle, structures=structures, knowledge_capacity=knowledge_capacity)
    updater = Updater(short_term=short_term, mid_term=mid_term, long_term=long_term, heat_threshold=threshold)
    return Lamp(
        handle=handle,
        structures=structures,
        short_term=short_term,
        mid_term=mid_term,
        long_term=long_term,
        updater=updater,
        system=MemoryOS(
            handle,
            structures=structures,
            short_term=short_term,
            mid_term=mid_term,
            long_term=long_term,
            updater=updater,
            retriever=Retriever(
                short_term=short_term,
                mid_term=mid_term,
                long_term=long_term,
                queue_capacity=queue_capacity,
            ),
        ),
    )


def _store() -> tuple[Memory, InMemoryBackend]:
    """Return one memory store and the backend its structures are built on."""

    backend = InMemoryBackend()
    return Memory(backend=backend, principal="memoryos-test"), backend


def _hours(offset: float) -> datetime:
    """Return a moment a given number of hours into the test scenario."""

    return START + timedelta(hours=offset)


def _exchange(
    user_input: str,
    *,
    hour: float,
    topic: str = SEATS,
    response: str = "Understood.",
    continues: bool = False,
    insight: Insight | None = None,
) -> Exchange:
    """Return one exchange at a fixed moment in the scenario."""

    return Exchange(
        user_input=user_input,
        agent_response=response,
        occurred_at=_hours(hour),
        topic=topic,
        continues=continues,
        insight=insight if insight is not None else Insight(),
    )


def _fill(lamp: Lamp, count: int, *, topic: str = SEATS, continues: bool = False, insight: Insight | None = None) -> list[Any]:
    """Record `count` exchanges one hour apart and return the results."""

    return [
        lamp.system.add_memory(
            _exchange(f"exchange number {index}", hour=index, topic=topic, continues=continues, insight=insight)
        )
        for index in range(count)
    ]


def test_structures_are_reopened_rather_than_duplicated() -> None:
    lamp = _lamp()

    again = MemoryOSStructures.create(lamp.handle, InMemoryBackend())

    assert again.short_term_root.id == lamp.structures.short_term_root.id
    assert again.mid_term_root.id == lamp.structures.mid_term_root.id
    assert again.user_root.id == lamp.structures.user_root.id
    assert again.assistant_root.id == lamp.structures.assistant_root.id
    assert len(lamp.handle.children()) == 4
    unbounded = MemoryHandle(memory=Memory(backend=InMemoryBackend()), root_id=None, principal=Principal("test"))
    with pytest.raises(ValueError, match="bounded"):
        MemoryOSStructures.create(unbounded, InMemoryBackend())


def test_each_tier_owns_only_the_blocks_that_belong_to_it() -> None:
    lamp = _lamp(capacity=1)
    _fill(
        lamp,
        2,
        insight=Insight(
            profile={"seat": "window"},
            user_knowledge=("Alice flies often.",),
            assistant_knowledge=("Ask before booking.",),
        ),
    )
    segment = lamp.mid_term.segments(now=_hours(1))[0]
    lamp.updater.promote(segment, now=_hours(1))

    sessions = lamp.handle.children(lamp.structures.mid_term_root)

    assert {block.type for block in lamp.handle.children(lamp.structures.short_term_root)} == {QA_TYPE}
    assert {block.type for block in sessions} == {SEGMENT_TYPE}
    assert {block.type for block in lamp.handle.children(sessions[0])} == {PAGE_TYPE}
    assert {block.type for block in lamp.handle.children(lamp.structures.user_root)} == {
        PROFILE_TYPE,
        REVISION_TYPE,
        KNOWLEDGE_TYPE,
    }
    assert {block.type for block in lamp.handle.children(lamp.structures.assistant_root)} == {KNOWLEDGE_TYPE}


def test_the_queue_keeps_arrival_order_and_reports_when_it_is_full() -> None:
    lamp = _lamp(capacity=3)

    _fill(lamp, 3)

    assert [entry.exchange.user_input for entry in lamp.short_term.entries()] == [
        "exchange number 0",
        "exchange number 1",
        "exchange number 2",
    ]
    assert lamp.short_term.is_full() is True
    assert lamp.short_term.size() == 3


def test_nothing_leaves_the_queue_until_the_caller_makes_room() -> None:
    lamp = _lamp(capacity=2)

    lamp.short_term.add(_exchange("first", hour=0))
    lamp.short_term.add(_exchange("second", hour=1))
    lamp.short_term.add(_exchange("third", hour=2))

    assert lamp.short_term.size() == 3
    assert lamp.mid_term.count() == 0


def test_popping_the_oldest_removes_it_from_an_append_only_log() -> None:
    lamp = _lamp(capacity=3)
    _fill(lamp, 3)

    popped = lamp.short_term.pop_oldest()

    assert popped is not None
    assert popped.exchange.user_input == "exchange number 0"
    assert popped.sequence == 0
    assert [entry.sequence for entry in lamp.short_term.entries()] == [1, 2]


def test_a_full_queue_pages_its_oldest_exchange_out_on_the_next_write() -> None:
    lamp = _lamp(capacity=3)

    results = _fill(lamp, 4)

    assert [page.exchange.user_input for page in results[3].paged_out] == ["exchange number 0"]
    assert [entry.exchange.user_input for entry in lamp.short_term.entries()] == [
        "exchange number 1",
        "exchange number 2",
        "exchange number 3",
    ]
    assert lamp.mid_term.count() == 1
    assert lamp.mid_term.page_count() == 1


def test_pages_with_the_same_topic_join_one_session() -> None:
    lamp = _lamp(capacity=2)

    _fill(lamp, 4, topic=SEATS)

    segments = lamp.mid_term.segments(now=_hours(4))
    assert len(segments) == 1
    assert segments[0].summary == SEATS
    assert len(segments[0].pages) == 2


def test_a_different_topic_opens_its_own_session() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("seats please", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("hotel please", hour=1, topic=HOTEL))
    lamp.system.add_memory(_exchange("meals please", hour=2, topic=MEALS))

    segments = lamp.mid_term.segments(now=_hours(3))

    assert [segment.summary for segment in segments] == [SEATS, HOTEL]
    assert [len(segment.pages) for segment in segments] == [1, 1]


def test_a_batch_must_clear_the_similarity_threshold_to_join_a_session() -> None:
    lamp = _lamp(capacity=1)
    lamp.mid_term.similarity_threshold = 5.0
    lamp.system.add_memory(_exchange("seats please", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("seats again", hour=1, topic=SEATS))
    lamp.system.add_memory(_exchange("seats once more", hour=2, topic=SEATS))

    segments = lamp.mid_term.segments(now=_hours(3))

    assert [segment.summary for segment in segments] == [SEATS, SEATS]


def test_length_counts_only_the_pages_no_analysis_has_read() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3)
    segment = lamp.mid_term.segments(now=_hours(3))[0]
    assert segment.length == 2

    lamp.mid_term.mark_analyzed(segment.pages[:1])

    reread = lamp.mid_term.segments(now=_hours(3))[0]
    assert len(reread.pages) == 2
    assert reread.length == 1


def test_heat_is_the_three_upstream_terms_and_nothing_else() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3)

    segment = lamp.mid_term.segments(now=_hours(2))[0]

    assert segment.visits == 0
    assert segment.length == 2
    assert segment.recency == pytest.approx(1.0)
    assert segment.heat == segment_heat(visits=0, length=2, recency=1.0)
    assert segment.heat == 3.0


def test_recency_decays_from_the_last_visit_time_in_the_temporal_index() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3)

    later = lamp.mid_term.segments(now=_hours(26))[0]

    assert later.last_visit_at == _hours(2)
    assert later.recency == pytest.approx(time_decay(_hours(2), _hours(26)))
    assert later.heat == pytest.approx(2.3679, abs=1e-4)


def test_a_retrieval_visits_the_sessions_it_reaches_and_makes_them_hotter() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3)
    before = lamp.mid_term.segments(now=_hours(2))[0]

    lamp.system.retrieve_memory("seat preferences", now=_hours(2))

    after = lamp.mid_term.segments(now=_hours(2))[0]
    assert before.visits == 0
    assert after.visits == 1
    assert after.lfu_count == before.lfu_count + 1
    assert after.heat > before.heat


def test_the_promotion_fires_when_the_hottest_session_reaches_the_threshold() -> None:
    lamp = _lamp(capacity=3, threshold=5.0)
    insight = Insight(profile={"seat": "window"}, user_knowledge=("Alice prefers window seats.",))
    lamp.system.add_memory(_exchange("first seat request", hour=0, insight=insight))
    lamp.system.add_memory(_exchange("second seat request", hour=0.1))
    lamp.system.add_memory(_exchange("third seat request", hour=0.2))
    lamp.system.add_memory(_exchange("fourth seat request", hour=0.3))
    lamp.system.retrieve_memory("seat request", now=_hours(0.35))
    lamp.system.add_memory(_exchange("fifth seat request", hour=0.4))

    promoting = lamp.system.add_memory(_exchange("sixth seat request", hour=0.5))

    assert promoting.promotion is not None
    assert promoting.promotion.heat >= 5.0
    assert promoting.promotion.profile_revision == 1
    assert [item.knowledge for item in promoting.promotion.user_knowledge] == ["Alice prefers window seats."]


def test_promotion_marks_its_pages_and_cools_the_session() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3, insight=Insight(user_knowledge=("Alice travels often.",)))
    segment = lamp.mid_term.segments(now=_hours(2))[0]

    promotion = lamp.updater.promote(segment, now=_hours(2))

    cooled = lamp.mid_term.segments(now=_hours(2))[0]
    assert promotion.analyzed_page_ids == tuple(page.page_id for page in segment.pages)
    assert all(page.analyzed for page in cooled.pages)
    assert cooled.visits == 0
    assert cooled.length == 0
    assert cooled.heat == 1.0
    assert promotion.cooled_heat == 1.0


def test_a_promotion_that_learns_nothing_still_cools_the_session() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3)
    segment = lamp.mid_term.segments(now=_hours(2))[0]

    promotion = lamp.updater.promote(segment, now=_hours(2))

    assert promotion.profile_revision == 0
    assert promotion.user_knowledge == ()
    assert lamp.mid_term.segments(now=_hours(2))[0].length == 0


def test_the_profile_merges_new_traits_over_the_ones_on_file() -> None:
    lamp = _lamp()

    lamp.long_term.update_profile({"seat": "aisle", "diet": "vegetarian"}, now=_hours(0))
    second = lamp.long_term.update_profile({"seat": "window"}, now=_hours(1))

    assert second is not None
    assert second.data == {"seat": "window", "diet": "vegetarian"}


def test_every_profile_an_analysis_wrote_stays_readable() -> None:
    lamp = _lamp()
    lamp.long_term.update_profile({"seat": "aisle"}, now=_hours(0))
    lamp.long_term.update_profile({"seat": "window"}, now=_hours(1))

    history = lamp.long_term.profile_history()

    current = lamp.system.get_user_profile()
    assert [revision.revision for revision in history] == [1, 2]
    assert [revision.data["seat"] for revision in history] == ["aisle", "window"]
    assert current is not None
    assert current.data == {"seat": "window"}


def test_an_analysis_that_changes_no_trait_writes_no_revision() -> None:
    lamp = _lamp()

    assert lamp.long_term.update_profile({}, now=_hours(0)) is None
    assert lamp.long_term.profile_history() == []


def test_forced_analysis_promotes_the_hottest_session_whatever_its_heat() -> None:
    lamp = _lamp(capacity=1, threshold=100.0)
    _fill(lamp, 2, insight=Insight(profile={"seat": "window"}))
    assert lamp.system.add_memory(_exchange("another", hour=3)).promotion is None

    promotion = lamp.system.force_mid_term_analysis(now=_hours(4))

    assert promotion is not None
    assert promotion.threshold == 0.0
    assert promotion.profile_revision == 1


def test_a_page_id_and_a_session_id_each_resolve_in_one_read() -> None:
    lamp = _lamp(capacity=1)
    result = _fill(lamp, 2)[1]
    page_id = result.paged_out[0].page_id
    segment_id = result.paged_out[0].segment_id

    page = lamp.mid_term.page(page_id)
    segment = lamp.mid_term.segment(segment_id, now=_hours(2))

    assert page is not None
    assert page.exchange.user_input == "exchange number 0"
    assert segment is not None
    assert segment.segment_id == segment_id
    assert lamp.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, page_id)) is not None
    assert lamp.structures.ids.get(MemoryAddress(SEGMENT_NAMESPACE, segment_id)) is not None
    assert lamp.mid_term.page("page_missing") is None


def test_the_dialogue_chain_survives_the_queue_that_held_it() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("opening question", hour=0))
    lamp.system.add_memory(_exchange("follow-up question", hour=1, continues=True))
    lamp.system.add_memory(_exchange("third question", hour=2, continues=True))
    lamp.system.add_memory(_exchange("fourth question", hour=3, continues=True))

    pages = lamp.mid_term.segments(now=_hours(4))[0].pages
    chain = lamp.mid_term.chain_from(pages[0].page_id)

    assert [page.exchange.user_input for page in pages] == ["opening question", "follow-up question", "third question"]
    assert [page.exchange.user_input for page in chain] == ["follow-up question", "third question"]


def test_an_exchange_that_starts_a_new_thread_is_not_chained() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("opening question", hour=0))
    lamp.system.add_memory(_exchange("unrelated question", hour=1, continues=False))
    lamp.system.add_memory(_exchange("third question", hour=2))

    pages = lamp.mid_term.segments(now=_hours(3))[0].pages

    assert len(pages) == 2
    assert lamp.mid_term.chain_from(pages[0].page_id) == []


def test_the_chain_refuses_an_edge_that_would_close_a_loop() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("opening question", hour=0))
    lamp.system.add_memory(_exchange("follow-up question", hour=1, continues=True))
    lamp.system.add_memory(_exchange("third question", hour=2, continues=True))
    pages = lamp.mid_term.segments(now=_hours(3))[0].pages

    with pytest.raises(MemoryConflict):
        lamp.mid_term.link_pages(pages[1], pages[0])


def test_the_least_frequently_used_session_is_evicted_with_its_pages() -> None:
    lamp = _lamp(capacity=1, mid_capacity=2)
    lamp.system.add_memory(_exchange("seats please", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("hotel please", hour=1, topic=HOTEL))
    lamp.system.retrieve_memory("flight seat preferences", now=_hours(1.5))
    lamp.system.add_memory(_exchange("meals please", hour=2, topic=MEALS))

    result = lamp.system.add_memory(_exchange("planning please", hour=3, topic="trip planning"))

    assert [eviction.reason for eviction in result.evictions] == ["least frequently used segment"]
    assert HOTEL in result.evictions[0].detail
    assert [segment.summary for segment in lamp.mid_term.segments(now=_hours(4))] == [SEATS, MEALS]
    assert lamp.mid_term.page_count() == 2


def test_evicting_a_session_frees_its_page_ids_rather_than_dangling_them() -> None:
    lamp = _lamp(capacity=1, mid_capacity=1)
    lamp.system.add_memory(_exchange("seats please", hour=0, topic=SEATS))
    first = lamp.system.add_memory(_exchange("hotel please", hour=1, topic=HOTEL))
    page_id = first.paged_out[0].page_id
    segment_id = first.paged_out[0].segment_id

    lamp.system.add_memory(_exchange("meals please", hour=2, topic=MEALS))

    assert lamp.mid_term.page(page_id) is None
    assert lamp.structures.ids.get(MemoryAddress(PAGE_NAMESPACE, page_id)) is None
    assert lamp.structures.ids.get(MemoryAddress(SEGMENT_NAMESPACE, segment_id)) is None


def test_the_chain_crosses_sessions_because_it_follows_the_dialogue() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("opening question", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("follow-up question", hour=1, topic=SEATS, continues=True))
    lamp.system.add_memory(_exchange("and about the hotel", hour=2, topic=HOTEL, continues=True))
    lamp.system.add_memory(_exchange("one more thing", hour=3, topic=HOTEL, continues=True))

    segments = lamp.mid_term.segments(now=_hours(4))
    chain = lamp.mid_term.chain_from(segments[0].pages[0].page_id)

    assert [segment.summary for segment in segments] == [SEATS, HOTEL]
    assert [page.exchange.user_input for page in chain] == ["follow-up question", "and about the hotel"]


def test_evicting_a_session_takes_the_chain_edges_its_pages_anchored() -> None:
    lamp = _lamp(capacity=1)
    lamp.system.add_memory(_exchange("opening question", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("follow-up question", hour=1, topic=SEATS, continues=True))
    lamp.system.add_memory(_exchange("and about the hotel", hour=2, topic=HOTEL, continues=True))
    lamp.system.add_memory(_exchange("one more thing", hour=3, topic=HOTEL, continues=True))
    surviving = lamp.mid_term.segments(now=_hours(4))[1].pages[0]
    assert lamp.structures.chain.predecessors(surviving.block) != []

    lamp.mid_term.evict_coldest()

    reread = lamp.mid_term.page(surviving.page_id)
    assert [segment.summary for segment in lamp.mid_term.segments(now=_hours(4))] == [HOTEL]
    assert reread is not None
    assert lamp.structures.chain.edges(reread.block) == []


def test_a_visited_session_outlives_a_colder_one_when_the_tier_fills() -> None:
    lamp = _lamp(capacity=1, mid_capacity=2)
    lamp.system.add_memory(_exchange("seats please", hour=0, topic=SEATS))
    lamp.system.add_memory(_exchange("hotel please", hour=1, topic=HOTEL))
    lamp.system.retrieve_memory("flight seat preferences", now=_hours(1.5))
    lamp.system.add_memory(_exchange("meals please", hour=2, topic=MEALS))

    lamp.system.add_memory(_exchange("planning please", hour=3, topic="trip planning"))

    summaries = {segment.summary for segment in lamp.mid_term.segments(now=_hours(4))}
    assert SEATS in summaries
    assert HOTEL not in summaries


def test_a_knowledge_base_at_capacity_drops_its_oldest_entry() -> None:
    lamp = _lamp(knowledge_capacity=2)

    lamp.long_term.add_knowledge(USER, "oldest fact", now=_hours(0))
    lamp.long_term.add_knowledge(USER, "middle fact", now=_hours(1))
    _, eviction = lamp.long_term.add_knowledge(USER, "newest fact", now=_hours(2))

    assert eviction is not None
    assert eviction.detail == "oldest fact"
    assert [item.knowledge for item in lamp.long_term.knowledge(USER)] == ["middle fact", "newest fact"]


def test_each_knowledge_base_is_written_and_read_on_its_own() -> None:
    lamp = _lamp()

    lamp.long_term.add_knowledge(USER, "Alice is vegetarian.", now=_hours(0))
    lamp.long_term.add_knowledge(ASSISTANT, "Confirm the seat first.", now=_hours(1))

    assert [item.knowledge for item in lamp.long_term.knowledge(USER)] == ["Alice is vegetarian."]
    assert [item.knowledge for item in lamp.long_term.knowledge(ASSISTANT)] == ["Confirm the seat first."]


def test_a_promoted_fact_names_the_page_it_was_read_from() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 3, insight=Insight(user_knowledge=("Alice travels often.",)))
    segment = lamp.mid_term.segments(now=_hours(2))[0]

    promotion = lamp.updater.promote(segment, now=_hours(2))

    stored = promotion.user_knowledge[0]
    assert stored.source_page_ids == (segment.pages[0].page_id,)
    assert [page.page_id for page in lamp.system.sources_of(stored.block)] == [segment.pages[0].page_id]


def test_provenance_edges_go_with_an_evicted_page_while_the_ids_stay_written() -> None:
    lamp = _lamp(capacity=1)
    _fill(lamp, 2, insight=Insight(user_knowledge=("Alice travels often.",)))
    segment = lamp.mid_term.segments(now=_hours(1))[0]
    stored = lamp.updater.promote(segment, now=_hours(1)).user_knowledge[0]

    lamp.mid_term.evict_coldest()

    assert stored.source_page_ids == (segment.pages[0].page_id,)
    assert lamp.system.sources_of(stored.block) == []
    assert lamp.long_term.knowledge(USER)[0].knowledge == "Alice travels often."


def test_retrieval_reads_every_tier_and_stops_at_the_queue_capacity() -> None:
    lamp = _lamp(capacity=1, queue_capacity=2)
    _fill(lamp, 4, insight=Insight(user_knowledge=("Alice keeps asking about seat preferences.",)))
    segment = lamp.mid_term.segments(now=_hours(3))[0]
    lamp.updater.promote(segment, now=_hours(3))

    context = lamp.system.retrieve_memory("seat preferences", now=_hours(4))

    assert len(context.short_term) == 1
    assert len(context.pages) == 2
    assert context.profile == {}
    assert [hit.item.knowledge for hit in context.user_knowledge] == ["Alice keeps asking about seat preferences."]
    assert context.assistant_knowledge == ()


def test_knowledge_search_keeps_only_entries_above_the_threshold() -> None:
    lamp = _lamp()
    lamp.long_term.add_knowledge(USER, "Alice prefers window seats on flights", now=_hours(0))
    lamp.long_term.add_knowledge(USER, "Alice keeps a folding bicycle", now=_hours(1))

    hits = lamp.long_term.search_knowledge(USER, "window seats", threshold=0.5)

    assert [hit.item.knowledge for hit in hits] == ["Alice prefers window seats on flights"]
    assert hits[0].score == pytest.approx(1.0)


def test_retrieval_searches_the_mid_term_tier_and_not_the_queue() -> None:
    lamp = _lamp(capacity=3)
    _fill(lamp, 4)

    context = lamp.system.retrieve_memory("seat preferences", now=_hours(4))

    assert [entry.exchange.user_input for entry in context.short_term] == [
        "exchange number 1",
        "exchange number 2",
        "exchange number 3",
    ]
    assert [hit.page.exchange.user_input for hit in context.pages] == ["exchange number 0"]


def test_a_tool_set_cannot_see_another_users_tiers() -> None:
    shared = _store()
    alice = _lamp(capacity=1, shared=shared, user_id="alice")
    bob = _lamp(capacity=1, shared=shared, user_id="bob")
    alice_tools = MemoryOSTools(alice.system, user_id="alice", assistant_id="travel-agent")
    bob_tools = MemoryOSTools(bob.system, user_id="bob", assistant_id="travel-agent")
    alice.system.add_memory(_exchange("Alice wants a window seat", hour=0))
    alice.system.add_memory(_exchange("Alice confirms the window seat", hour=1))

    stats = bob_tools.call("memory_stats", {})
    found = bob_tools.call("retrieve_memory", {"query": "window seat", "at": "2025-04-08T11:00:00Z"})

    assert stats["mid_term_pages"] == 0
    assert found["retrieved_pages"] == []
    assert alice_tools.call("memory_stats", {})["mid_term_pages"] == 1


def test_tools_return_json_compatible_results_for_every_call() -> None:
    lamp = _lamp(capacity=1)
    tools = MemoryOSTools(lamp.system, user_id="alice", assistant_id="travel-agent")
    first = tools.call(
        "add_memory",
        {
            "user_input": "I want a window seat",
            "agent_response": "Window seats noted",
            "timestamp": "2025-04-08T09:00:00Z",
            "topic": SEATS,
            "insight": {"profile": {"seat": "window"}, "user_knowledge": ["Alice prefers window seats"]},
        },
    )
    second = tools.call(
        "add_memory",
        {
            "user_input": "Confirm the window seat",
            "agent_response": "Confirmed",
            "timestamp": "2025-04-08T10:00:00Z",
            "topic": SEATS,
            "continues": True,
        },
    )
    page_id = second["paged_out"][0]["page_id"]
    promoted = tools.call("analyze_now", {"at": "2025-04-08T11:00:00Z"})
    knowledge_id = promoted["promoted"]["user_knowledge"][0]["id"]

    results = {
        "retrieve_memory": tools.call("retrieve_memory", {"query": "window seat", "at": "2025-04-08T11:30:00Z"}),
        "get_user_profile": tools.call("get_user_profile", {}),
        "inspect_sessions": tools.call("inspect_sessions", {"at": "2025-04-08T11:30:00Z"}),
        "trace_knowledge": tools.call("trace_knowledge", {"owner": USER, "knowledge_id": knowledge_id}),
        "read_dialogue_chain": tools.call("read_dialogue_chain", {"page_id": page_id, "depth": 3}),
        "memory_stats": tools.call("memory_stats", {}),
    }

    assert first["promotion"] is None
    assert _is_json(first) and _is_json(second) and _is_json(promoted)
    assert all(_is_json(result) for result in results.values())
    assert results["get_user_profile"]["profile"] == {"seat": "window"}
    assert results["trace_knowledge"]["pages"][0]["page_id"] == page_id
    assert results["read_dialogue_chain"]["found"] is True
    assert results["inspect_sessions"]["hottest"] is not None
    with pytest.raises(ValueError, match="Unknown MemoryOS tool"):
        tools.call("forget_everything", {})


def test_the_agent_promotes_once_on_its_own_and_loses_the_session_nobody_revisited() -> None:
    agent = build_agent()

    found = run_conversation(agent)
    answer = report(agent, found)

    promotions = [record for record in agent.call_log if record.name == "add_memory" and record.result["promotion"]]
    evictions = [
        eviction
        for record in agent.call_log
        if record.name == "add_memory"
        for eviction in record.result["evictions"]
    ]
    assert len(promotions) == 1
    assert promotions[0].result["promotion"]["H_segment"] == 5.0
    assert promotions[0].result["promotion"]["cooled_H_segment"] == 1.0
    forced = next(record.result["promoted"] for record in agent.call_log if record.name == "analyze_now")
    assert [eviction["detail"] for eviction in evictions] == ["Austin hotel choices (2 page(s))"]
    assert forced["user_profile_revision"] == 2
    assert [eviction["detail"] for eviction in forced["evictions"]] == ["Alice prefers aisle seats on flights."]
    assert "Profile on file (revision 2):\n- seat_preference: window" in answer
    assert "- document_watch: passport expires March 2026" in answer
    assert "Why 'Alice switched from aisle seats to window seats.' is on file:" in answer
    assert "- read from: 'The window seat turned out better, so keep window from now on.'" in answer
    assert "- then: Last trip you booked a window seat and I had to have it changed." in answer
    assert "- 3/3 sessions holding 5 page(s)" in answer


def test_the_agent_uses_every_tool_it_was_given() -> None:
    agent = build_agent()

    found = run_conversation(agent)
    report(agent, found)

    assert set(agent.tool_usage()) == {definition.name for definition in agent.tools.definitions}
    assert agent.tool_usage() == {
        "add_memory": 10,
        "retrieve_memory": 2,
        "inspect_sessions": 2,
        "analyze_now": 1,
        "trace_knowledge": 1,
        "read_dialogue_chain": 1,
        "get_user_profile": 1,
        "memory_stats": 1,
    }


def test_the_demo_reaches_the_threshold_from_a_read_rather_than_a_write() -> None:
    agent = build_agent()

    run_conversation(agent)

    tables = [record.result for record in agent.call_log if record.name == "inspect_sessions"]
    assert tables[0]["threshold"] == 5.0
    assert tables[0]["sessions"][0]["N_visit"] == 1
    assert tables[0]["sessions"][0]["L_interaction"] == 2
    assert tables[0]["sessions"][0]["H_segment"] < 5.0
    assert [session["summary"] for session in tables[1]["sessions"]] == [
        "flight seat preferences",
        "meal requirements",
        "passport and documents",
    ]


def test_demo_uses_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("== MemoryOS-inspired tiered memory ==\n")
    assert "Genie structures: ownership tree, three event logs, keyed collection" in output
    assert "\nTools available to the agent:\n" in output
    assert "\nAgent answer:\nQuestion:" in output
    assert "Inspectable tool calls:\n01. add_memory\n02. add_memory" in output
    assert "Tool usage:\n- add_memory x10\n- retrieve_memory x2\n" in output


def _is_json(value: object) -> bool:
    """Return whether a tool result contains only JSON-compatible values."""

    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_is_json(item) for item in value)
    return value is None or isinstance(value, str | int | float | bool)


def test_agent_records_isolated_copies_of_every_call() -> None:
    lamp = _lamp(capacity=3)
    agent = MemoryOSAgent(name="travel-agent", tools=MemoryOSTools(lamp.system, user_id="alice", assistant_id="travel-agent"))

    agent.record(_exchange("I want a window seat", hour=0))
    agent.record(_exchange("Confirm the window seat", hour=1, continues=True))
    agent.call_log[0].arguments["user_input"] = "tampered"

    assert lamp.short_term.entries()[0].exchange.user_input == "I want a window seat"
    assert agent.call_log[1].arguments["user_input"] == "Confirm the window seat"
