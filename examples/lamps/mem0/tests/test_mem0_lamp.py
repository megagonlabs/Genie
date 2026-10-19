"""Tests for the Mem0-inspired Genie lamp."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from examples.lamps.mem0.agent import Mem0Tools
from examples.lamps.mem0.client import Mem0Memory
from examples.lamps.mem0.demo import answer_question, build_agent, main, teach_agent
from examples.lamps.mem0.graph import GraphMemory
from examples.lamps.mem0.history import HistoryLog
from examples.lamps.mem0.model import (
    FACTUAL,
    MEMORY_NAMESPACE,
    MEMORY_TYPE,
    PROCEDURAL,
    READ_LIMIT,
    SCOPE_TYPE,
    STORE_TYPE,
    Fact,
    Message,
    Session,
    Triplet,
)
from examples.lamps.mem0.pipeline import FactExtractor, MemoryUpdater
from examples.lamps.mem0.store import MemoryStore
from examples.lamps.mem0.structures import Mem0Structures
from genie import InMemoryBackend, Memory, MemoryHandle, Principal
from genie.structures import MemoryAddress

ALICE = Session(user_id="alice", agent_id="travel-agent", run_id="trip-42")
ALICE_LATER = Session(user_id="alice", agent_id="travel-agent", run_id="trip-77")
ALICE_USER = Session(user_id="alice")
BOB = Session(user_id="bob", agent_id="travel-agent", run_id="trip-99")

MONDAY = datetime(2025, 4, 10, 9, tzinfo=UTC)
FRIDAY = datetime(2025, 4, 14, 9, tzinfo=UTC)

AISLE = "Alice prefers aisle seats on flights"
WINDOW = "Alice prefers window seats on flights"
DIET = "Alice is vegetarian"
LAYOVERS = "Alice avoids overnight layovers"


@dataclass(frozen=True, slots=True)
class Lamp:
    """The connected parts that one test uses."""

    handle: MemoryHandle
    structures: Mem0Structures
    store: MemoryStore
    history: HistoryLog
    graph: GraphMemory
    client: Mem0Memory


def _lamp(*, threshold: float = 0.5) -> Lamp:
    """Build an empty lamp over one in-memory backend."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="mem0-test")
    root = memory.put(type=STORE_TYPE, value={"name": "test"})
    handle = memory.get_handle(root=root)
    structures = Mem0Structures.create(handle, backend)
    store = MemoryStore(handle, structures=structures)
    history = HistoryLog(handle, structures=structures)
    graph = GraphMemory(handle, structures=structures)
    return Lamp(
        handle=handle,
        structures=structures,
        store=store,
        history=history,
        graph=graph,
        client=Mem0Memory(
            handle,
            store=store,
            history_log=history,
            graph=graph,
            extractor=FactExtractor(),
            updater=MemoryUpdater(threshold=threshold),
        ),
    )


def _turn(content: str, *, role: str = "user", name: str = "alice") -> Message:
    """Return one conversation turn."""

    return Message(role, content, name=name)


def test_a_session_needs_an_identifier_and_mirrors_the_ones_it_has_as_a_path() -> None:
    assert ALICE.path == ("user:alice", "agent:travel-agent", "run:trip-42")
    assert ALICE.label == "user:alice/agent:travel-agent/run:trip-42"
    assert ALICE_USER.path == ("user:alice",)
    assert Session(run_id="trip-42").path == ("run:trip-42",)
    assert ALICE.with_actor("alice").identifiers["actor_id"] == "alice"
    with pytest.raises(ValueError, match="at least one of"):
        Session()


def test_the_first_exchange_adds_every_candidate_because_nothing_is_on_file() -> None:
    lamp = _lamp()

    result = lamp.client.add(
        ALICE,
        (_turn("Aisle seat again, vegetarian meal, no overnight layovers."),),
        facts=(Fact(AISLE), Fact(DIET), Fact(LAYOVERS)),
        at=MONDAY,
    )

    assert [change.event for change in result.changes] == ["ADD", "ADD", "ADD"]
    assert [change.memory.text for change in result.changes if change.memory] == [AISLE, DIET, LAYOVERS]
    assert all(change.memory is not None and change.memory.version == 1 for change in result.changes)
    assert [memory.text for memory in lamp.client.get_all(ALICE)] == [LAYOVERS, DIET, AISLE]


def test_a_closer_wording_updates_the_stored_memory_in_place_and_keeps_its_id() -> None:
    lamp = _lamp()
    first = lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)
    original = first.changes[0].memory
    assert original is not None

    second = lamp.client.add(ALICE, facts=(Fact(WINDOW),), at=FRIDAY)
    updated = second.changes[0].memory

    assert second.changes[0].event == "UPDATE"
    assert second.changes[0].previous_text == AISLE
    assert updated is not None
    assert updated.memory_id == original.memory_id
    assert (original.version, updated.version) == (1, 2)
    assert updated.created_at == original.created_at
    assert updated.updated_at == FRIDAY
    assert lamp.client.get(original.memory_id) == updated


def test_a_repeated_fact_writes_nothing_and_leaves_no_history_row() -> None:
    lamp = _lamp()
    first = lamp.client.add(ALICE, facts=(Fact(DIET),), at=MONDAY)
    stored = first.changes[0].memory
    assert stored is not None

    second = lamp.client.add(ALICE, facts=(Fact(DIET),), at=FRIDAY)

    assert second.changes[0].event == "NONE"
    assert second.changes[0].decision.reason == "the memory is already stored word for word"
    assert second.changes[0].memory is None
    assert [entry.event for entry in lamp.client.history(stored.memory_id)] == ["ADD"]
    assert lamp.client.get(stored.memory_id) == stored


def test_a_contradicting_fact_deletes_the_memory_it_contradicts() -> None:
    lamp = _lamp()
    first = lamp.client.add(ALICE, facts=(Fact(LAYOVERS),), at=MONDAY)
    stored = first.changes[0].memory
    assert stored is not None

    second = lamp.client.add(ALICE, facts=(Fact("Alice no longer avoids overnight layovers", retracts=True),), at=FRIDAY)

    assert second.changes[0].event == "DELETE"
    assert second.changes[0].previous_text == LAYOVERS
    assert lamp.client.get(stored.memory_id) is None
    assert lamp.client.get_all(ALICE) == []


def test_a_retraction_with_nothing_similar_on_file_writes_nothing() -> None:
    lamp = _lamp()

    result = lamp.client.add(ALICE, facts=(Fact("Alice no longer takes the night train", retracts=True),), at=MONDAY)

    assert result.changes[0].event == "NONE"
    assert result.changes[0].decision.reason == "nothing close enough is on file to retract"
    assert lamp.client.get_all(ALICE) == []


def test_the_update_phase_sees_memories_added_earlier_in_the_same_call() -> None:
    lamp = _lamp()

    result = lamp.client.add(ALICE, facts=(Fact(AISLE), Fact(WINDOW)), at=MONDAY)

    assert [change.event for change in result.changes] == ["ADD", "UPDATE"]
    assert [memory.text for memory in lamp.client.get_all(ALICE)] == [WINDOW]


def test_the_similarity_floor_decides_between_folding_in_and_adding_beside() -> None:
    strict = _lamp(threshold=0.9)
    loose = _lamp(threshold=0.2)
    for lamp in (strict, loose):
        lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)

    strict_result = strict.client.add(ALICE, facts=(Fact(WINDOW),), at=FRIDAY)
    loose_result = loose.client.add(ALICE, facts=(Fact(DIET),), at=FRIDAY)

    assert strict_result.changes[0].event == "ADD"
    assert loose_result.changes[0].event == "ADD"
    assert strict_result.changes[0].decision.score == 0.7143
    with pytest.raises(ValueError, match="threshold"):
        MemoryUpdater(threshold=0.0)


def test_a_memory_id_is_a_keyed_address_that_needs_no_scope_to_resolve() -> None:
    lamp = _lamp()
    result = lamp.client.add(ALICE, facts=(Fact(DIET),), at=MONDAY)
    stored = result.changes[0].memory
    assert stored is not None

    address = MemoryAddress(MEMORY_NAMESPACE, stored.memory_id)

    assert lamp.structures.records.get(address) == stored.block
    assert stored.block.type == MEMORY_TYPE
    assert lamp.client.get(stored.memory_id) == stored
    assert lamp.client.get("nobody-stored-this") is None
    with pytest.raises(KeyError, match="was not found"):
        lamp.store.require("nobody-stored-this")


def test_a_wider_session_reads_the_memories_of_every_run_below_it() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)
    lamp.client.add(ALICE_LATER, facts=(Fact(DIET),), at=FRIDAY)
    lamp.client.add(BOB, facts=(Fact("Bob prefers the night train"),), at=MONDAY)

    assert [memory.text for memory in lamp.client.get_all(ALICE)] == [AISLE]
    assert [memory.text for memory in lamp.client.get_all(ALICE_LATER)] == [DIET]
    assert [memory.text for memory in lamp.client.get_all(ALICE_USER)] == [AISLE, DIET]
    assert [memory.text for memory in lamp.client.get_all(BOB)] == ["Bob prefers the night train"]


def test_one_run_reconciles_only_against_its_own_scope() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)

    result = lamp.client.add(ALICE_LATER, facts=(Fact(WINDOW),), at=FRIDAY)

    assert result.changes[0].event == "ADD"
    assert [memory.text for memory in lamp.client.get_all(ALICE_USER)] == [AISLE, WINDOW]


def test_search_scopes_to_the_session_path_and_filters_on_metadata() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(AISLE), Fact(DIET)), at=MONDAY)
    lamp.client.add(ALICE, facts=(Fact("Alice flies out of Austin on Mondays"),), memory_type=PROCEDURAL, at=MONDAY)
    lamp.client.add(BOB, facts=(Fact("Bob prefers aisle seats on flights"),), at=MONDAY)

    found = lamp.client.search(ALICE_USER, "Alice aisle seats", filters={"memory_type": FACTUAL})
    filtered = lamp.client.search(ALICE_USER, "Alice Austin Mondays", filters={"memory_type": PROCEDURAL})
    lifted = lamp.client.search(ALICE_USER, "Alice aisle seats", threshold=1.0)

    assert [hit.memory.text for hit in found.hits] == [AISLE, DIET]
    assert [hit.score for hit in found.hits] == [1.0, 0.3333]
    assert found.hits[0].matched == ("alice", "aisle", "seats")
    assert [hit.memory.text for hit in filtered.hits] == ["Alice flies out of Austin on Mondays"]
    assert [hit.memory.text for hit in lifted.hits] == [AISLE]
    assert [hit.memory.text for hit in lamp.client.search(BOB, "aisle seats").hits] == ["Bob prefers aisle seats on flights"]


def test_search_returns_the_relationships_the_query_names_beside_the_memories() -> None:
    lamp = _lamp()
    lamp.client.add(
        ALICE,
        facts=(Fact(DIET),),
        relations=(Triplet("Alice", "diet", "vegetarian"), Triplet("Riverside Inn", "located in", "Austin")),
        at=MONDAY,
    )

    found = lamp.client.search(ALICE_USER, "Alice is vegetarian")

    assert [hit.memory.text for hit in found.hits] == [DIET]
    assert [relation.as_json() for relation in found.relations] == [
        {"source": "Alice", "relationship": "diet", "destination": "vegetarian", "retracted": False}
    ]


def test_a_retracted_relationship_is_marked_rather_than_removed() -> None:
    lamp = _lamp()
    added = lamp.client.add(ALICE, relations=(Triplet("Alice", "prefers", "aisle seat"),), at=MONDAY)

    repeated = lamp.client.add(ALICE, relations=(Triplet("Alice", "prefers", "aisle seat"),), at=FRIDAY)
    retracted = lamp.client.add(ALICE, relations=(Triplet("Alice", "prefers", "aisle seat", retracts=True),), at=FRIDAY)
    missing = lamp.client.add(ALICE, relations=(Triplet("Alice", "prefers", "sleeper cabin", retracts=True),), at=FRIDAY)

    assert [change.event for change in added.relations] == ["ADD"]
    assert [change.event for change in repeated.relations] == ["NONE"]
    assert [change.event for change in retracted.relations] == ["DELETE"]
    assert [change.event for change in missing.relations] == ["NONE"]
    assert lamp.client.relations(ALICE) == []
    assert [relation.retracted for relation in lamp.client.relations(ALICE, include_retracted=True)] == [True]


def test_entities_resolve_by_folded_name_inside_the_outermost_identifier() -> None:
    lamp = _lamp()

    lamp.client.add(ALICE, relations=(Triplet("Alice", "diet", "vegetarian"),), at=MONDAY)
    lamp.client.add(ALICE_LATER, relations=(Triplet("ALICE", "stays at", "Riverside Inn"),), at=FRIDAY)

    assert lamp.graph.namespace(ALICE) == lamp.graph.namespace(ALICE_LATER) == ("user:alice",)
    assert lamp.graph.entity_names(ALICE) == ["Alice", "Riverside Inn", "vegetarian"]
    assert [relation.as_json()["relationship"] for relation in lamp.client.relations(ALICE_USER)] == ["diet", "stays at"]
    assert lamp.graph.entity(BOB, "Alice") is None


def test_every_change_is_recorded_once_per_memory_and_once_in_the_journal() -> None:
    lamp = _lamp()
    first = lamp.client.add(ALICE, facts=(Fact(AISLE), Fact(LAYOVERS)), at=MONDAY)
    seat = first.changes[0].memory
    layover = first.changes[1].memory
    assert seat is not None and layover is not None

    lamp.client.add(
        ALICE,
        (_turn("Window seats now, layovers are fine.", name="alice"),),
        facts=(Fact(WINDOW), Fact("Alice no longer avoids overnight layovers", retracts=True)),
        at=FRIDAY,
    )

    assert [(entry.revision, entry.event) for entry in lamp.client.history(seat.memory_id)] == [(1, "ADD"), (2, "UPDATE")]
    assert [(entry.revision, entry.event) for entry in lamp.client.history(layover.memory_id)] == [(1, "ADD"), (2, "DELETE")]
    assert [(entry.sequence, entry.event) for entry in lamp.client.journal()] == [
        (0, "ADD"),
        (1, "ADD"),
        (2, "UPDATE"),
        (3, "DELETE"),
    ]
    assert sorted(lamp.history.recorded_ids()) == sorted([seat.memory_id, layover.memory_id])


def test_a_history_row_carries_the_wording_it_replaced_and_who_said_it() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, (_turn(AISLE, name="alice"),), facts=(Fact(AISLE),), at=MONDAY)

    lamp.client.add(ALICE, (_turn(WINDOW, name="alice"),), facts=(Fact(WINDOW),), at=FRIDAY)
    events = lamp.client.journal()

    assert events[1].old_memory == AISLE
    assert events[1].new_memory == WINDOW
    assert events[1].actor_id == "alice"
    assert events[1].role == "user"
    assert events[1].is_deleted is False
    assert events[0].old_memory is None


def test_a_deleted_memory_keeps_a_readable_history_ending_in_the_deletion() -> None:
    lamp = _lamp()
    result = lamp.client.add(ALICE, facts=(Fact(DIET),), at=MONDAY)
    stored = result.changes[0].memory
    assert stored is not None

    change = lamp.client.delete(stored.memory_id, actor_id="alice", at=FRIDAY)
    events = lamp.client.history(stored.memory_id)

    assert change.event == "DELETE"
    assert change.memory == stored
    assert lamp.client.get(stored.memory_id) is None
    assert [entry.event for entry in events] == ["ADD", "DELETE"]
    assert events[-1].is_deleted is True
    assert events[-1].old_memory == DIET
    assert events[-1].new_memory is None


def test_a_direct_rewrite_is_recorded_the_same_way_a_decided_one_is() -> None:
    lamp = _lamp()
    result = lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)
    stored = result.changes[0].memory
    assert stored is not None

    change = lamp.client.update(stored.memory_id, WINDOW, metadata={"source": "correction"}, actor_id="alice", at=FRIDAY)

    assert change.event == "UPDATE"
    assert change.decision.reason == "the caller rewrote the memory directly"
    assert change.memory is not None
    assert change.memory.version == 2
    assert change.memory.metadata == {"source": "correction"}
    assert [entry.event for entry in lamp.client.history(stored.memory_id)] == ["ADD", "UPDATE"]
    with pytest.raises(KeyError, match="was not found"):
        lamp.client.update("nobody-stored-this", WINDOW, at=FRIDAY)


def test_retiring_a_run_is_one_subtree_delete_that_leaves_a_sibling_alone() -> None:
    lamp = _lamp()
    kept = lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY).changes[0].memory
    dropped = lamp.client.add(ALICE_LATER, facts=(Fact(DIET),), at=FRIDAY).changes[0].memory
    assert kept is not None and dropped is not None

    removed = lamp.client.delete_all(ALICE_LATER, at=FRIDAY)

    assert [change.memory.text for change in removed if change.memory] == [DIET]
    assert lamp.store.scope_node(ALICE_LATER) is None
    assert lamp.structures.records.get(MemoryAddress(MEMORY_NAMESPACE, dropped.memory_id)) is None
    assert lamp.client.get(dropped.memory_id) is None
    assert lamp.client.get(kept.memory_id) == kept
    assert [entry.event for entry in lamp.client.history(dropped.memory_id)] == ["ADD", "DELETE"]
    assert lamp.client.delete_all(ALICE_LATER, at=FRIDAY) == []


def test_retiring_a_user_removes_every_run_below_them_at_once() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)
    lamp.client.add(ALICE_LATER, facts=(Fact(DIET),), at=FRIDAY)
    bob = lamp.client.add(BOB, facts=(Fact("Bob prefers the night train"),), at=MONDAY).changes[0].memory
    assert bob is not None

    removed = lamp.client.delete_all(ALICE_USER, at=FRIDAY)

    assert len(removed) == 2
    assert lamp.client.get_all(ALICE_USER) == []
    assert lamp.client.get(bob.memory_id) == bob


def test_retiring_a_user_also_clears_their_graph_half() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(DIET),), relations=(Triplet("Alice", "diet", "vegetarian"),), at=MONDAY)
    lamp.client.add(BOB, relations=(Triplet("Bob", "prefers", "night train"),), at=MONDAY)

    kept = lamp.client.delete_all(ALICE_LATER, at=FRIDAY)
    dropped = lamp.client.delete_all(ALICE_USER, at=FRIDAY)

    assert kept == []
    assert len(dropped) == 1
    assert lamp.graph.entity_names(ALICE_USER) == []
    assert lamp.client.relations(ALICE_USER, include_retracted=True) == []
    assert [relation.as_json()["source"] for relation in lamp.client.relations(BOB)] == ["Bob"]


def test_retiring_one_run_leaves_the_user_wide_entities_alone() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE_LATER, facts=(Fact(DIET),), relations=(Triplet("Alice", "diet", "vegetarian"),), at=MONDAY)

    lamp.client.delete_all(ALICE_LATER, at=FRIDAY)

    assert lamp.client.get_all(ALICE_USER) == []
    assert lamp.graph.entity_names(ALICE_USER) == ["Alice", "vegetarian"]


def test_entity_nodes_are_owned_by_their_namespace() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, relations=(Triplet("Alice", "diet", "vegetarian"),), at=MONDAY)
    lamp.client.add(BOB, relations=(Triplet("Bob", "prefers", "night train"),), at=MONDAY)

    scopes = lamp.structures.tiers.children(lamp.structures.entity_root)

    assert [block.metadata["segment"] for block in scopes] == ["user:alice", "user:bob"]
    assert all(block.type == SCOPE_TYPE for block in scopes)
    assert [block.metadata["entity"] for block in lamp.structures.tiers.children(scopes[1])] == ["bob", "night train"]


def test_retiring_a_namespace_clears_more_entities_than_one_read_returns() -> None:
    lamp = _lamp()
    for index in range(READ_LIMIT + 5):
        lamp.graph.ensure_entity(ALICE_USER, f"entity-{index:05d}")

    assert len(lamp.graph.entities(ALICE_USER, limit=READ_LIMIT)) == READ_LIMIT

    lamp.graph.drop(ALICE_USER)

    assert lamp.structures.tiers.children(lamp.structures.entity_root) == []
    assert lamp.graph.entity_names(ALICE_USER) == []


def test_storing_an_exchange_verbatim_skips_both_phases() -> None:
    lamp = _lamp()

    result = lamp.client.add(
        ALICE,
        (
            _turn("Is the Austin hotel refundable?"),
            _turn("Yes, up to two days before check-in.", role="assistant", name="travel-agent"),
            _turn("Ignore this.", role="system", name="system"),
        ),
        infer=False,
        at=MONDAY,
    )

    assert [change.event for change in result.changes] == ["ADD", "ADD"]
    assert [change.decision.reason for change in result.changes] == [
        "stored as said, without extraction",
        "stored as said, without extraction",
    ]
    assert [memory.session.actor_id for memory in lamp.client.get_all(ALICE)] == ["alice", "travel-agent"]
    assert [memory.text for memory in lamp.client.get_all(ALICE)] == [
        "Is the Austin hotel refundable?",
        "Yes, up to two days before check-in.",
    ]


def test_a_procedural_write_stores_one_summary_without_consulting_the_store() -> None:
    lamp = _lamp()
    lamp.client.add(ALICE, facts=(Fact(AISLE),), at=MONDAY)

    result = lamp.client.add(ALICE, facts=(Fact(AISLE),), memory_type=PROCEDURAL, at=FRIDAY)
    summary = result.changes[0].memory

    assert result.changes[0].event == "ADD"
    assert summary is not None
    assert summary.kind == PROCEDURAL
    assert summary.block.metadata["memory_type"] == PROCEDURAL
    assert len(lamp.client.get_all(ALICE)) == 2
    with pytest.raises(ValueError, match="exactly one summary"):
        lamp.client.add(ALICE, facts=(Fact(AISLE), Fact(DIET)), memory_type=PROCEDURAL, at=FRIDAY)


def test_the_extractor_credits_a_fact_to_the_last_turn_and_drops_repeats() -> None:
    extractor = FactExtractor()
    turns = (_turn("Window seats now."), _turn("Confirmed.", role="assistant", name="travel-agent"))

    extracted = extractor.extract(turns, (Fact(WINDOW), Fact(WINDOW), Fact(DIET, actor_id="alice", role="user")))
    verbatim = extractor.verbatim(turns)

    assert [fact.text for fact in extracted] == [WINDOW, DIET]
    assert extracted[0].actor_id == "travel-agent"
    assert extracted[0].role == "assistant"
    assert extracted[1].actor_id == "alice"
    assert [fact.actor_id for fact in verbatim] == ["alice", "travel-agent"]


def test_a_tool_set_is_bound_to_one_user_and_cannot_be_argued_out_of_it() -> None:
    lamp = _lamp()
    alice = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent"))
    bob = Mem0Tools(lamp.client, session=Session(user_id="bob", agent_id="travel-agent"))
    alice.call("add_memories", {"facts": [{"text": AISLE}], "at": "2025-04-10T09:00:00Z", "run": "trip-42"})

    assert alice.scope().label == "user:alice/agent:travel-agent"
    assert alice.scope(run="trip-42").label == "user:alice/agent:travel-agent/run:trip-42"
    assert len(alice.call("search_memories", {"query": "Alice aisle seats"})["results"]) == 1
    assert bob.call("search_memories", {"query": "Alice aisle seats"})["results"] == []
    assert bob.call("list_memories", {})["results"] == []
    with pytest.raises(ValueError, match="Unknown Mem0 tool"):
        alice.call("reset_everything", {})


def test_a_memory_id_from_another_scope_is_refused_like_an_unknown_one() -> None:
    lamp = _lamp()
    alice = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent"))
    bob = Mem0Tools(lamp.client, session=Session(user_id="bob", agent_id="travel-agent"))
    added = alice.call("add_memories", {"facts": [{"text": AISLE}], "at": "2025-04-10T09:00:00Z", "run": "trip-42"})
    memory_id = added["results"][0]["id"]

    assert alice.owns(memory_id) is True
    assert bob.owns(memory_id) is False
    assert bob.owns("nobody-stored-this") is False
    assert bob.call("memory_history", {"memory_id": memory_id}) == {"memory_id": memory_id, "exists": False, "events": []}
    assert bob.call("memory_history", {"memory_id": "nobody-stored-this"}) == {
        "memory_id": "nobody-stored-this",
        "exists": False,
        "events": [],
    }
    with pytest.raises(KeyError, match="was not found"):
        bob.call("update_memory", {"memory_id": memory_id, "text": "Bob rewrote this", "at": "2025-04-14T09:00:00Z"})
    with pytest.raises(KeyError, match="was not found"):
        bob.call("delete_memory", {"memory_id": memory_id, "at": "2025-04-14T09:00:00Z"})

    assert [memory["memory"] for memory in alice.call("list_memories", {})["results"]] == [AISLE]


def test_the_bound_scope_reaches_every_run_below_it_and_nothing_beside_it() -> None:
    lamp = _lamp()
    agent_wide = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent"))
    one_run = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent", run_id="trip-99"))
    added = agent_wide.call("add_memories", {"facts": [{"text": AISLE}], "at": "2025-04-10T09:00:00Z", "run": "trip-42"})
    memory_id = added["results"][0]["id"]

    assert agent_wide.owns(memory_id) is True
    assert one_run.owns(memory_id) is False
    assert agent_wide.call("update_memory", {"memory_id": memory_id, "text": WINDOW, "at": "2025-04-14T09:00:00Z"})["event"] == "UPDATE"


def test_a_history_stays_readable_and_scoped_after_the_memory_is_deleted() -> None:
    lamp = _lamp()
    alice = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent", run_id="trip-42"))
    bob = Mem0Tools(lamp.client, session=Session(user_id="bob", agent_id="travel-agent"))
    added = alice.call("add_memories", {"facts": [{"text": AISLE}], "at": "2025-04-10T09:00:00Z"})
    memory_id = added["results"][0]["id"]
    alice.call("delete_memory", {"memory_id": memory_id, "at": "2025-04-14T09:00:00Z"})

    after = alice.call("memory_history", {"memory_id": memory_id})

    assert lamp.history.owner(memory_id) == ("user:alice", "agent:travel-agent", "run:trip-42")
    assert lamp.client.owner(memory_id) == ("user:alice", "agent:travel-agent", "run:trip-42")
    assert lamp.client.owner("nobody-stored-this") is None
    assert after["exists"] is False
    assert [event["event"] for event in after["events"]] == ["ADD", "DELETE"]
    assert bob.call("memory_history", {"memory_id": memory_id})["events"] == []


def test_tools_return_json_compatible_results_for_every_call() -> None:
    lamp = _lamp()
    tools = Mem0Tools(lamp.client, session=Session(user_id="alice", agent_id="travel-agent", run_id="trip-42"))
    added = tools.call(
        "add_memories",
        {
            "messages": [{"role": "user", "content": "Aisle seat again.", "name": "alice"}],
            "facts": [{"text": AISLE}],
            "relations": [{"source": "Alice", "relationship": "prefers", "destination": "aisle seat"}],
            "at": "2025-04-10T09:00:00Z",
        },
    )
    memory_id = added["results"][0]["id"]

    history = tools.call("memory_history", {"memory_id": memory_id})
    deleted = tools.call("delete_memory", {"memory_id": memory_id, "at": "2025-04-14T09:00:00Z"})
    after = tools.call("memory_history", {"memory_id": memory_id})
    retired = tools.call("forget_scope", {"at": "2025-04-14T09:00:00Z"})

    assert added["scope"] == "user:alice/agent:travel-agent/run:trip-42"
    assert added["results"][0]["event"] == "ADD"
    assert added["relations"][0]["event"] == "ADD"
    assert history["exists"] is True
    assert [event["event"] for event in history["events"]] == ["ADD"]
    assert deleted["event"] == "DELETE"
    assert after["exists"] is False
    assert [event["event"] for event in after["events"]] == ["ADD", "DELETE"]
    assert retired["removed"] == []


def test_structures_are_reopened_rather_than_duplicated() -> None:
    lamp = _lamp()

    again = Mem0Structures.create(lamp.handle, InMemoryBackend())

    assert again.memory_root.id == lamp.structures.memory_root.id
    assert again.history_root.id == lamp.structures.history_root.id
    assert again.entity_root.id == lamp.structures.entity_root.id
    assert len(lamp.handle.children()) == 3
    unbounded = MemoryHandle(memory=Memory(backend=InMemoryBackend()), root_id=None, principal=Principal("test"))
    with pytest.raises(ValueError, match="bounded"):
        Mem0Structures.create(unbounded, InMemoryBackend())


def test_the_agent_runs_every_write_path_and_answers_from_both_halves(capsys) -> None:
    agent = build_agent()
    memory_id = teach_agent(agent)

    answer = answer_question(agent, memory_id)

    assert "- Alice prefers window seats on flights (v2, score 0.6)" in answer
    assert "- Alice stays at the Cedar Lodge when visiting Austin (v2, score 0.4)" in answer
    assert "- Alice prefers window seat" in answer
    assert "- ADD: nothing on file -> Alice prefers aisle seats on flights" in answer
    assert "- UPDATE: Alice prefers aisle seats on flights -> Alice prefers window seats on flights" in answer
    assert [call.name for call in agent.call_log] == [
        "add_memories",
        "add_memories",
        "add_memories",
        "add_memories",
        "update_memory",
        "delete_memory",
        "memory_history",
        "forget_scope",
        "search_memories",
        "list_memories",
        "memory_history",
    ]
    assert agent.tool_usage() == {
        "add_memories": 4,
        "update_memory": 1,
        "delete_memory": 1,
        "memory_history": 2,
        "forget_scope": 1,
        "search_memories": 1,
        "list_memories": 1,
    }
    assert capsys.readouterr().out == ""


def test_the_demo_reconciles_the_second_exchange_into_three_different_events() -> None:
    agent = build_agent()
    teach_agent(agent)
    calls = dict(enumerate(agent.call_log))

    assert [entry["event"] for entry in calls[0].result["results"]] == ["ADD"] * 5
    assert [entry["event"] for entry in calls[1].result["results"]] == ["UPDATE", "NONE", "DELETE"]
    assert [entry["event"] for entry in calls[1].result["relations"]] == ["DELETE", "ADD"]
    assert calls[1].result["results"][0]["previous_memory"] == "Alice prefers aisle seats on flights"
    assert calls[3].result["results"][0]["reason"] == "stored as said, without extraction"
    assert calls[6].result["exists"] is False
    assert [event["event"] for event in calls[6].result["events"]] == ["ADD", "DELETE"]
    assert [entry["memory"] for entry in calls[7].result["removed"]] == [
        "Quick one: is the Austin hotel refundable?",
        "Yes, up to two days before check-in.",
    ]


def test_demo_uses_the_shared_terminal_format(capsys) -> None:
    main()

    output = capsys.readouterr().out
    assert output.startswith("== Mem0-inspired scoped memory ==\n")
    assert "Genie structures: ownership tree, two keyed collections, versioned records" in output
    assert "\nTools available to the agent:\n" in output
    assert "\nAgent answer:\nQuestion:" in output
    assert "Inspectable tool calls:\n01. add_memories\n02. add_memories" in output
    assert "Tool usage:\n- add_memories x4\n- update_memory x1\n" in output
