"""Run a travel assistant with tiered memory, based on MemoryOS.

From the repository root:

    python -m examples.lamps.memoryos.demo
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from examples.lamps.demo_output import print_agent_demo
from examples.lamps.memoryos.agent import MemoryOSAgent, MemoryOSTools
from examples.lamps.memoryos.long_term import LongTermMemory
from examples.lamps.memoryos.memoryos import MemoryOS
from examples.lamps.memoryos.mid_term import MidTermMemory
from examples.lamps.memoryos.model import SYSTEM_TYPE, USER, Exchange, Insight
from examples.lamps.memoryos.retriever import Retriever
from examples.lamps.memoryos.short_term import ShortTermMemory
from examples.lamps.memoryos.structures import MemoryOSStructures
from examples.lamps.memoryos.updater import Updater
from genie import InMemoryBackend, Memory

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
    """Return one time on the day of the demo.

    Args:
        hour: Hour of the day in UTC.
        minute: Minute of the hour.

    Returns:
        A fixed timestamp with a timezone. So no run reads a clock.
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
    """Build the hierarchy of one user and an agent bound to it.

    The capacities are much smaller than the MemoryOS defaults, so the demo
    reaches each boundary:

    - **Queue:** it overflows after 3 exchanges.
    - **Paged tier:** it holds 3 sessions.
    - **Knowledge bases:** each one holds 2 facts.
    - **Retrieval:** it keeps at most 3 mid-term pages.
    - **Heat threshold:** 5.0, the upstream value. One session gets to it with
      no forced analysis.

    Returns:
        An agent that uses memory only through the named tool set.
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
    """Talk to the agent until the conversation crossed each tier boundary.

    - **Promotion from heat:** the 3 seat exchanges page into one session, and
      one retrieval visits it. The third page brings its heat to the threshold
      exactly. So the hierarchy decides the promotion. The agent does not call
      for it.
    - **Eviction:** later, the lamp evicts the hotel session before it is ever
      hot enough for an analysis. This is the other half of a heat rule.

    Args:
        agent: Agent to talk to.

    Returns:
        The page id of the first paged-out exchange, and the ids of the facts
        that the first promotion wrote. The report reads them back later.
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
    """Get the answer of the agent, which traces one promoted fact and one chain.

    Args:
        agent: Agent that the demo talked to.
        found: Page id and knowledge ids that the conversation collected.

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
    """Run the full deterministic agent workflow of the MemoryOS lamp."""

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
    """Return the page id of the first exchange that left the queue in this call.

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
    """Return the ids of the facts that one promotion wrote to a knowledge base.

    Args:
        result: Result of the `add_memory` call whose heat check caused a
            promotion.
        key: `user_knowledge` or `assistant_knowledge`.

    Returns:
        The block ids, in write order.

    Raises:
        KeyError: If the call promoted nothing.
    """

    promotion = result["promotion"]
    if promotion is None:
        raise KeyError("This call promoted nothing into long-term memory.")
    return tuple(str(entry["id"]) for entry in promotion[key])


if __name__ == "__main__":
    main()
