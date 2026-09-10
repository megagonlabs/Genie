"""Run a travel assistant with MemoryOS-inspired tiered memory.

From the repository root:

    python -m lamps.memoryos.demo
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.memoryos.agent import MemoryOSAgent, MemoryOSTools
from lamps.memoryos.long_term import LongTermMemory
from lamps.memoryos.memoryos import MemoryOS
from lamps.memoryos.mid_term import MidTermMemory
from lamps.memoryos.model import SYSTEM_TYPE, USER, Exchange, Insight
from lamps.memoryos.retriever import Retriever
from lamps.memoryos.short_term import ShortTermMemory
from lamps.memoryos.structures import MemoryOSStructures
from lamps.memoryos.updater import Updater

USER_ID = "alice"
ASSISTANT_ID = "travel-agent"

SHORT_TERM_CAPACITY = 3
MID_TERM_CAPACITY = 3
KNOWLEDGE_CAPACITY = 2
HEAT_THRESHOLD = 5.0

SEATS = "flight seat preferences"
HOTEL = "Austin hotel choices"
MEALS = "meal requirements"
DOCUMENTS = "passport and documents"
PLANNING = "trip planning"

QUESTION = "How should you book Alice's next Austin trip?"
QUERY = "window seat booking passport Austin"


def at(hour: int, minute: int = 0) -> datetime:
    """Return one moment on the afternoon the demo takes place.

    Args:
        hour: Hour of the day in UTC.
        minute: Minute of the hour.

    Returns:
        A timezone-aware timestamp, so no run reads a clock.
    """

    return datetime(2025, 4, 8, hour, minute, tzinfo=UTC)


CONVERSATION: tuple[Exchange, ...] = (
    Exchange(
        user_input="I always want an aisle seat when we fly.",
        agent_response="Noted, I will book aisle seats for you.",
        occurred_at=at(9),
        topic=SEATS,
        insight=Insight(
            profile={"seat_preference": "aisle"},
            user_knowledge=("Alice prefers aisle seats on flights.",),
        ),
    ),
    Exchange(
        user_input="Last trip you booked a window seat and I had to have it changed.",
        agent_response="I will confirm the seat with you before booking from now on.",
        occurred_at=at(9, 20),
        topic=SEATS,
        continues=True,
        insight=Insight(assistant_knowledge=("Confirm Alice's seat preference before booking.",)),
    ),
    Exchange(
        user_input="The window seat turned out better, so keep window from now on.",
        agent_response="Window seats from now on.",
        occurred_at=at(9, 40),
        topic=SEATS,
        continues=True,
        insight=Insight(
            profile={"seat_preference": "window"},
            user_knowledge=("Alice switched from aisle seats to window seats.",),
        ),
    ),
    Exchange(
        user_input="In Austin I stay at the Riverside Inn.",
        agent_response="I will hold the Riverside Inn for Austin trips.",
        occurred_at=at(10),
        topic=HOTEL,
        insight=Insight(
            profile={"austin_hotel": "Riverside Inn"},
            user_knowledge=("Alice stays at the Riverside Inn in Austin.",),
        ),
    ),
    Exchange(
        user_input="Is the Riverside Inn refundable?",
        agent_response="Yes, up to two days before check-in.",
        occurred_at=at(10, 30),
        topic=HOTEL,
        continues=True,
    ),
    Exchange(
        user_input="I am vegetarian, on every leg.",
        agent_response="Vegetarian meals on every leg.",
        occurred_at=at(11),
        topic=MEALS,
        insight=Insight(user_knowledge=("Alice is vegetarian.",)),
    ),
    Exchange(
        user_input="My passport expires in March 2026.",
        agent_response="I will watch the passport date for you.",
        occurred_at=at(11, 30),
        topic=DOCUMENTS,
        insight=Insight(
            profile={"document_watch": "passport expires March 2026"},
            user_knowledge=("Alice's passport expires in March 2026.",),
        ),
    ),
    Exchange(
        user_input="Book the vegetarian meal for the Austin flight.",
        agent_response="Vegetarian meal booked.",
        occurred_at=at(12),
        topic=MEALS,
    ),
    Exchange(
        user_input="Remind me to renew the passport in January.",
        agent_response="Reminder set for January.",
        occurred_at=at(12, 30),
        topic=DOCUMENTS,
        insight=Insight(assistant_knowledge=("Set a passport renewal reminder for January.",)),
    ),
    Exchange(
        user_input="Can you start planning next month's Austin trip?",
        agent_response="I will draft an itinerary from your preferences.",
        occurred_at=at(13),
        topic=PLANNING,
    ),
)


def build_agent() -> MemoryOSAgent:
    """Assemble one user's hierarchy and an agent bound to it.

    The capacities are far below MemoryOS's defaults so the demo reaches every
    boundary: the queue overflows after three exchanges, the paged tier holds
    three sessions, and each knowledge base holds two facts. The heat threshold
    is upstream's own 5.0, and a session gets there on its own.

    Returns:
        An agent whose only memory access is the named tool set.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="memoryos-demo")
    root = memory.put(type=SYSTEM_TYPE, value={"user_id": USER_ID, "assistant_id": ASSISTANT_ID})
    handle = memory.get_handle(root=root)
    structures = MemoryOSStructures.create(handle, backend)
    short_term = ShortTermMemory(handle, structures=structures, capacity=SHORT_TERM_CAPACITY)
    mid_term = MidTermMemory(handle, structures=structures, capacity=MID_TERM_CAPACITY)
    long_term = LongTermMemory(handle, structures=structures, knowledge_capacity=KNOWLEDGE_CAPACITY)
    system = MemoryOS(
        handle,
        structures=structures,
        short_term=short_term,
        mid_term=mid_term,
        long_term=long_term,
        updater=Updater(
            short_term=short_term,
            mid_term=mid_term,
            long_term=long_term,
            heat_threshold=HEAT_THRESHOLD,
        ),
        retriever=Retriever(short_term=short_term, mid_term=mid_term, long_term=long_term, queue_capacity=3),
    )
    return MemoryOSAgent(
        name=ASSISTANT_ID,
        tools=MemoryOSTools(system, user_id=USER_ID, assistant_id=ASSISTANT_ID),
    )


def run_conversation(agent: MemoryOSAgent) -> dict[str, Any]:
    """Talk to the agent until every tier boundary has been crossed.

    - The three seat exchanges page into one session, one retrieval visits it,
      and the third page takes its heat to exactly the threshold. So the
      promotion is the hierarchy's own decision rather than a call the agent
      made.
    - Later the hotel session is evicted before it was ever hot enough to be
      read, which is the other half of what a heat rule means.

    Args:
        agent: Agent to talk to.

    Returns:
        The page id of the first paged-out exchange and the ids of the facts the
        first promotion wrote, which the report reads back afterwards.
    """

    opening = [agent.record(exchange) for exchange in CONVERSATION[:4]]
    agent.recall("aisle window seat change booking", at=at(10, 15))
    agent.record(CONVERSATION[4])
    agent.review(at=at(10, 45))
    promoting = agent.record(CONVERSATION[5])
    for exchange in CONVERSATION[6:]:
        agent.record(exchange)
    agent.review(at=at(13, 15))
    agent.analyze(at=at(13, 20))
    return {
        "first_page": _first_page(opening[3]),
        "user_facts": _promoted_facts(promoting, "user_knowledge"),
    }


def report(agent: MemoryOSAgent, found: dict[str, Any]) -> str:
    """Have the agent answer, tracing one promoted fact and one chain on the way.

    Args:
        agent: Agent that has been talked to.
        found: Page and knowledge ids collected while talking.

    Returns:
        The readable answer.
    """

    return agent.answer(
        QUESTION,
        query=QUERY,
        at=at(13, 30),
        knowledge_id=found["user_facts"][-1],
        chain_from=found["first_page"],
        owner=USER,
    )


def main() -> None:
    """Run the complete deterministic MemoryOS-inspired agent workflow."""

    agent = build_agent()
    found = run_conversation(agent)
    answer = report(agent, found)
    print_agent_demo(
        title="MemoryOS-inspired tiered memory",
        purpose=(
            "An agent talks through a full memory hierarchy: exchanges page out "
            "of a short queue into topic sessions, the session a retrieval keeps "
            "visiting gets hot enough to be read into a lasting profile, and the "
            "one nobody comes back to is evicted before it ever is."
        ),
        structures=(
            "ownership tree",
            "three event logs",
            "keyed collection",
            "temporal collection",
            "lineage DAG",
            "versioned records",
            "property graph",
        ),
        tools=agent.tools.definitions,
        answer=answer,
        calls=agent.call_log,
    )


def _first_page(result: dict[str, Any]) -> str:
    """Return the page id of the first exchange that was paged out.

    Args:
        result: Result of the `add_memory` call that caused the overflow.

    Returns:
        The page id.

    Raises:
        KeyError: If the call paged nothing out.
    """

    pages = result["paged_out"]
    if not pages:
        raise KeyError("This call paged nothing out of short-term memory.")
    return str(pages[0]["page_id"])


def _promoted_facts(result: dict[str, Any], key: str) -> tuple[str, ...]:
    """Return the ids of the facts one promotion wrote to a knowledge base.

    Args:
        result: Result of the `add_memory` call whose heat check promoted.
        key: Either `user_knowledge` or `assistant_knowledge`.

    Returns:
        The block ids, in the order they were written.

    Raises:
        KeyError: If the call promoted nothing.
    """

    promotion = result["promotion"]
    if promotion is None:
        raise KeyError("This call promoted nothing into long-term memory.")
    return tuple(str(entry["id"]) for entry in promotion[key])


if __name__ == "__main__":
    main()
