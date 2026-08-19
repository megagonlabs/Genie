"""Run an agent with Mem0-inspired memory tools.

From the repository root:

    conda run -n "genie" python -m lamps.mem0.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.mem0.agent import (
    Mem0Agent,
    Mem0Tools,
    MemoryCandidate,
    MemoryUpdateCandidate,
    MentalModelCandidate,
)
from lamps.mem0.memory_store import Mem0Memory, Mem0Structures, MemoryScope


def build_demo_agent() -> Mem0Agent:
    """Create an agent that receives scope-bound tools, not raw memory."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="mem0-demo")
    root = memory.put(type="mem0.memory_collection", value={"name": "travel assistant"})
    handle = memory.get_handle(root=root)
    store = Mem0Memory(
        handle,
        structures=Mem0Structures.create(handle, backend),
    )
    scope = MemoryScope(
        user_id="alice",
        agent_id="travel-agent",
        run_id="planning-session-42",
    )
    return Mem0Agent(name="travel-agent", tools=Mem0Tools(store, scope=scope))


def teach_demo_agent(agent: Mem0Agent) -> None:
    """Have the agent add facts and experiences, revise one, and synthesize."""

    agent.learn(
        memories=(
            MemoryCandidate(
                key="seat_preference",
                text="Alice usually chose aisle seats on flights.",
                kind="fact",
                entities=("Alice", "flight"),
                metadata={"source": "profile-v1"},
            ),
            MemoryCandidate(
                key="hotel_preference",
                text="Alice prefers Riverside Inn for trips to Austin.",
                kind="fact",
                entities=("Alice", "Riverside Inn", "Austin"),
                metadata={"source": "conversation-40"},
            ),
            MemoryCandidate(
                key="seat_problem",
                text="I booked Alice an aisle seat; she asked me to change it.",
                kind="experience",
                entities=("Alice", "flight"),
                occurred_at="2025-04-10T09:00:00Z",
                metadata={"source": "agent-run-41", "outcome": "correction requested"},
            ),
            MemoryCandidate(
                key="seat_fix",
                text="I changed Alice's reservation to a window seat.",
                kind="experience",
                entities=("Alice", "flight"),
                occurred_at="2025-04-11T10:30:00Z",
                metadata={"source": "agent-run-41", "outcome": "accepted"},
            ),
        ),
        updates=(
            MemoryUpdateCandidate(
                key="seat_preference",
                text="Alice now prefers window seats on flights.",
                metadata={"source": "conversation-41", "supersedes": "profile-v1"},
            ),
        ),
        mental_model=MentalModelCandidate(
            name="alice-travel-profile",
            source_query="How should I plan Alice's trips?",
            content=(
                "Prioritize a window seat for Alice and use Riverside Inn for Austin "
                "when availability and price are suitable."
            ),
            source_keys=("seat_preference", "hotel_preference", "seat_fix"),
        ),
    )


def answer_demo_question(agent: Mem0Agent) -> str:
    """Have the agent combine four retrieval paths into one answer."""

    return agent.answer(
        "How should I plan Alice's next trip, and what happened last time?",
        query="Alice prefers window seats flights",
        start="2025-04-10T00:00:00Z",
        end="2025-04-11T23:59:59Z",
        graph_anchor="seat_problem",
        mental_model_name="alice-travel-profile",
    )


def main() -> None:
    """Run the complete deterministic agent workflow."""

    agent = build_demo_agent()
    teach_demo_agent(agent)
    answer = answer_demo_question(agent)
    print_agent_demo(
        title="Mem0-inspired scoped memory",
        purpose=(
            "An agent stores scoped fact records, updates preferences, and "
            "retrieves context by text, time, and graph relationships."
        ),
        structures=("keyed collection", "temporal collection", "property graph"),
        tools=agent.tools,
        answer=answer,
        calls=agent.tool_calls,
    )


if __name__ == "__main__":
    main()
