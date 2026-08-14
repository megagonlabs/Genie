"""Run an agent with MemoryOS-inspired memory tools.

From the repository root:

    conda run -n "genie" python -m lamps.memoryos.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.memoryos.agent import (
    ConversationCandidate,
    KnowledgeCandidate,
    LongTermCandidate,
    MemoryOSAgent,
    MemoryOSTools,
)
from lamps.memoryos.memory_system import MemoryOSMemory, MemoryOSStructures


def build_demo_agent() -> MemoryOSAgent:
    """Create an agent over a small three-tier memory hierarchy."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="memoryos-demo")
    root = memory.put(
        type="memoryos.user_memory",
        value={"user_id": "alice", "assistant_id": "travel-agent"},
    )
    handle = memory.get_handle(root=root)
    hierarchy = MemoryOSMemory(
        handle,
        structures=MemoryOSStructures.create(handle, backend),
        short_term_capacity=2,
        heat_threshold=2.0,
        knowledge_capacity=10,
    )
    return MemoryOSAgent(name="travel-agent", tools=MemoryOSTools(hierarchy))


def teach_demo_agent(agent: MemoryOSAgent) -> None:
    """Have the agent exercise FIFO promotion, segment heat, and long-term updates."""

    agent.learn(
        conversations=(
            ConversationCandidate(
                key="seat-preference",
                user_input="Please remember that I prefer window seats.",
                agent_response="I'll prioritize window seats for future flights.",
                occurred_at="2025-04-08T09:00:00Z",
                summary="Alice's flight seat preference is window.",
                entities=("Alice", "flight"),
            ),
            ConversationCandidate(
                key="aisle-problem",
                user_input="Last time an aisle seat was booked and I asked to change it.",
                agent_response="I should check your current seat preference before booking.",
                occurred_at="2025-04-09T09:30:00Z",
                summary="A prior aisle-seat booking required a correction.",
                entities=("Alice", "flight"),
            ),
            ConversationCandidate(
                key="hotel-preference",
                user_input="For Austin trips, I like Riverside Inn.",
                agent_response="I'll consider Riverside Inn when planning Austin travel.",
                occurred_at="2025-04-10T08:00:00Z",
                summary="Alice prefers Riverside Inn for Austin trips.",
                entities=("Alice", "Riverside Inn", "Austin"),
            ),
            ConversationCandidate(
                key="seat-fix",
                user_input="The window-seat change worked well.",
                agent_response="Great, I'll use that successful correction as guidance.",
                occurred_at="2025-04-11T10:30:00Z",
                summary="The window-seat correction succeeded.",
                entities=("Alice", "flight"),
            ),
            ConversationCandidate(
                key="next-trip",
                user_input="Help me plan my next trip.",
                agent_response="I'll check your saved preferences and prior experience first.",
                occurred_at="2025-04-12T11:00:00Z",
                summary="Alice asked the assistant to plan another trip.",
                entities=("Alice",),
            ),
        ),
        reflection_query="Alice travel seat preference booking correction Riverside Austin",
        long_term=LongTermCandidate(
            profile_updates={
                "seat_preference": "window",
                "austin_hotel": "Riverside Inn",
                "planning_strategy": "confirm current preferences before booking",
            },
            profile_source_keys=("seat-preference", "aisle-problem", "hotel-preference"),
            user_knowledge=(
                KnowledgeCandidate(
                    knowledge="Alice prefers window seats for flights.",
                    source_keys=("seat-preference",),
                    entities=("Alice", "flight"),
                ),
                KnowledgeCandidate(
                    knowledge="Alice prefers Riverside Inn for Austin trips.",
                    source_keys=("hotel-preference",),
                    entities=("Alice", "Riverside Inn", "Austin"),
                ),
            ),
            assistant_knowledge=(
                KnowledgeCandidate(
                    knowledge="Check the user's current seat preference before booking.",
                    source_keys=("aisle-problem",),
                    entities=("flight",),
                ),
            ),
        ),
    )


def answer_demo_question(agent: MemoryOSAgent) -> str:
    """Have the agent retrieve all tiers, a timeline, and graph context."""

    return agent.answer(
        "How should you plan Alice's next trip, and what happened previously?",
        query="Alice travel window seat Riverside booking preference",
        start="2025-04-08T00:00:00Z",
        end="2025-04-12T23:59:59Z",
        graph_anchor_key="aisle-problem",
    )


def main() -> None:
    """Run the deterministic MemoryOS-inspired agent workflow."""

    agent = build_demo_agent()
    teach_demo_agent(agent)
    answer = answer_demo_question(agent)
    print_agent_demo(
        title="MemoryOS-inspired tiered memory",
        purpose=(
            "An agent moves conversations through short-, mid-, and long-term "
            "memory, then retrieves across all tiers."
        ),
        structures=(
            "ownership tree",
            "keyed collection",
            "event logs",
            "temporal collection",
            "property graph",
            "lineage DAG",
        ),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
