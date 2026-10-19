"""Run a travel agent with memory tools based on the design of Mem0.

From the repository root:

    python -m examples.lamps.mem0.demo
"""

from __future__ import annotations

from typing import Any

from examples.lamps.demo_output import print_agent_demo
from examples.lamps.mem0.agent import Mem0Agent, Mem0Tools
from examples.lamps.mem0.client import Mem0Memory
from examples.lamps.mem0.graph import GraphMemory
from examples.lamps.mem0.history import HistoryLog
from examples.lamps.mem0.model import STORE_TYPE, Exchange, Fact, Message, Session, Triplet
from examples.lamps.mem0.pipeline import FactExtractor, MemoryUpdater
from examples.lamps.mem0.store import MemoryStore
from examples.lamps.mem0.structures import Mem0Structures
from genie import InMemoryBackend, Memory

USER = "alice"
ASSISTANT = "travel-agent"
PLANNING_RUN = "trip-42"
TRANSCRIPT_RUN = "trip-77"

FIRST_TURN_AT = "2025-04-10T09:00:00Z"
SECOND_TURN_AT = "2025-04-12T14:30:00Z"
SUMMARIZED_AT = "2025-04-12T15:00:00Z"
TRANSCRIBED_AT = "2025-05-02T10:15:00Z"
CORRECTED_AT = "2025-05-02T10:20:00Z"
RETIRED_AT = "2025-05-10T08:00:00Z"

FIRST_EXCHANGE = Exchange(
    messages=(
        Message("user", "Book me the aisle seat again, and no overnight layovers please.", name=USER),
        Message("assistant", "Noted. I will also keep your usual Austin hotel and meal preference."),
    ),
    facts=(
        Fact("Alice prefers aisle seats on flights"),
        Fact("Alice is vegetarian"),
        Fact("Alice avoids overnight layovers"),
        Fact("Alice stays at the Riverside Inn when visiting Austin"),
        Fact("Alice's passport expires in March 2026"),
    ),
    relations=(
        Triplet("Alice", "prefers", "aisle seat"),
        Triplet("Alice", "diet", "vegetarian"),
        Triplet("Alice", "stays at", "Riverside Inn"),
    ),
)

SECOND_EXCHANGE = Exchange(
    messages=(
        Message("user", "I have switched to window seats, and overnight layovers are fine now.", name=USER),
        Message("assistant", "Understood. Still vegetarian meals?"),
        Message("user", "Yes, I am vegetarian.", name=USER),
    ),
    facts=(
        Fact("Alice prefers window seats on flights"),
        Fact("Alice is vegetarian"),
        Fact("Alice no longer avoids overnight layovers", retracts=True),
    ),
    relations=(
        Triplet("Alice", "prefers", "aisle seat", retracts=True),
        Triplet("Alice", "prefers", "window seat"),
    ),
)

PLANNING_SUMMARY = (
    "To plan a trip for Alice: confirm the seat preference first, then hold the Austin hotel, "
    "then add the vegetarian meal to every leg."
)

TRANSCRIPT = Exchange(
    messages=(
        Message("user", "Quick one: is the Austin hotel refundable?", name=USER),
        Message("assistant", "Yes, up to two days before check-in."),
    )
)

CORRECTION = "Alice stays at the Cedar Lodge when visiting Austin"

QUESTION = "How should I book Alice's next Austin trip?"
QUERY = "Alice window seats vegetarian Austin"


def build_agent() -> Mem0Agent:
    """Build an empty store, its structures, and an agent over them.

    - **Bound scope:** the tool set is bound to one user and one agent. Thus no
      tool argument can name the memories of a different user.
    - **Id check:** the client below keeps the flat id space of upstream. Thus
      the tools that take a memory id check it against the bound scope.
    - **Run as an argument:** a run is a tool argument, because upstream a run
      is one conversation and the caller gives it for each call.

    Returns:
        An agent that uses memory only through the named tool set.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="mem0-demo")
    root = memory.put(type=STORE_TYPE, value={"name": "travel assistant"})
    handle = memory.get_handle(root=root)
    structures = Mem0Structures.create(handle, backend)
    client = Mem0Memory(
        handle,
        store=MemoryStore(handle, structures=structures),
        history_log=HistoryLog(handle, structures=structures),
        graph=GraphMemory(handle, structures=structures),
        extractor=FactExtractor(),
        updater=MemoryUpdater(threshold=0.5, neighbors=5),
    )
    return Mem0Agent(
        name=ASSISTANT,
        tools=Mem0Tools(client, session=Session(user_id=USER, agent_id=ASSISTANT)),
    )


def teach_agent(agent: Mem0Agent) -> str:
    """Run each write path of Mem0 and return the id of the seat memory.

    - **First exchange:** no stored memories exist for a comparison. Thus the
      update phase adds all 5 candidates.
    - **Second exchange:** the update phase has work to do. The new seat
      preference replaces the old one in place. The repeated diet fact writes
      nothing. The layover retraction removes the memory that it contradicts.
      The same turn retracts one graph relationship and adds a different one.
    - **Paths that skip the pipeline:** one procedural summary of the task
      steps, and a later conversation in its own run and in its original words.
      Then the caller, not the pipeline, changes 2 memories. It corrects one
      memory in place and deletes the other memory.
    - **History read:** the agent reads the history of the deleted layover
      memory.
    - **Last step:** one call retires the transcript run.

    Args:
        agent: Agent to teach.

    Returns:
        Id of the seat-preference memory. That memory is now at its second
        version.
    """

    first = agent.learn(FIRST_EXCHANGE, at=FIRST_TURN_AT, run=PLANNING_RUN, metadata={"source": "chat"})
    second = agent.learn(SECOND_EXCHANGE, at=SECOND_TURN_AT, run=PLANNING_RUN, metadata={"source": "chat"})
    agent.summarize(SECOND_EXCHANGE, PLANNING_SUMMARY, at=SUMMARIZED_AT, run=PLANNING_RUN)
    agent.transcribe(TRANSCRIPT, at=TRANSCRIBED_AT, run=TRANSCRIPT_RUN)
    agent.revise(_id(first, "Alice stays at the Riverside Inn when visiting Austin"), CORRECTION, at=CORRECTED_AT, actor=USER)
    agent.discard(_id(first, "Alice's passport expires in March 2026"), at=CORRECTED_AT, actor=USER)
    agent.inspect(_removed(second))
    agent.forget(at=RETIRED_AT, run=TRANSCRIPT_RUN)
    return _id(second, "Alice prefers window seats on flights")


def answer_question(agent: Mem0Agent, memory_id: str) -> str:
    """Make the agent answer from the 2 halves of its memory and one history.

    Args:
        agent: Agent after `teach_agent`.
        memory_id: Memory whose recorded changes the answer shows.

    Returns:
        The readable answer.
    """

    return agent.answer(QUESTION, query=QUERY, memory_id=memory_id, limit=3)


def main() -> None:
    """Run the full deterministic agent workflow based on Mem0."""

    agent = build_agent()
    memory_id = teach_agent(agent)
    answer = answer_question(agent, memory_id)
    print_agent_demo(
        title="Mem0-inspired scoped memory",
        purpose=(
            "An agent hands conversations to a two-phase pipeline that decides "
            "whether each extracted fact is added, folded into a stored memory, "
            "or allowed to delete one, and every decision stays readable."
        ),
        structures=(
            "ownership tree",
            "two keyed collections",
            "versioned records",
            "event log",
            "property graph",
        ),
        tools=agent.tools.definitions,
        answer=answer,
        calls=agent.call_log,
    )


def _id(result: dict[str, Any], memory: str) -> str:
    """Return the id of the memory with a given text that one add call wrote.

    Args:
        result: Result of an `add_memories` call.
        memory: Memory text to find.

    Returns:
        The memory id.

    Raises:
        KeyError: If the call wrote no memory with that text.
    """

    for entry in result["results"]:
        if entry["memory"] == memory and entry["id"] is not None:
            return str(entry["id"])
    raise KeyError(f"No memory {memory!r} was written by this call.")


def _removed(result: dict[str, Any]) -> str:
    """Return the id of the memory that one add call deleted.

    Args:
        result: Result of an `add_memories` call.

    Returns:
        The id of the deleted memory.

    Raises:
        KeyError: If the call deleted no memory.
    """

    for entry in result["results"]:
        if entry["event"] == "DELETE" and entry["id"] is not None:
            return str(entry["id"])
    raise KeyError("This call deleted no memory.")


if __name__ == "__main__":
    main()
