"""Run an agent with LangMem-inspired memory tools.

From the repository root:

    conda run -n "genie" python -m lamps.langmem.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.langmem.agent import (
    LangMemAgent,
    LangMemTools,
    MemoryCandidate,
    MemoryLinkCandidate,
)
from lamps.langmem.memory_store import (
    LangMemStore,
    LangMemStructures,
    Namespace,
    ValueSchema,
)

BASE_NAMESPACE: Namespace = ("travel-assistant", "alice")


def build_demo_agent() -> LangMemAgent:
    """Create an agent with namespace-bound tools and three supplied schemas."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="langmem-demo")
    root = memory.put(type="langmem.store", value={"name": "Alice travel memory"})
    handle = memory.get_handle(root=root)
    schemas = {
        (*BASE_NAMESPACE, "semantic"): ValueSchema(
            "semantic_fact",
            {"subject": str, "predicate": str, "object": str, "context": str},
        ),
        (*BASE_NAMESPACE, "episodes"): ValueSchema(
            "episode",
            {"observation": str, "thoughts": str, "action": str, "result": str},
        ),
        (*BASE_NAMESPACE, "profiles"): ValueSchema(
            "user_profile",
            {"name": str, "summary": str, "principles": list, "source_keys": list},
        ),
    }
    store = LangMemStore(
        handle,
        structures=LangMemStructures.create(handle, backend),
        schemas=schemas,
    )
    return LangMemAgent(
        name="travel-agent",
        tools=LangMemTools(store, base_namespace=BASE_NAMESPACE),
    )


def teach_demo_agent(agent: LangMemAgent) -> None:
    """Have the agent retain facts, episodes, a semantic profile, and relations."""

    agent.learn(
        memories=(
            MemoryCandidate(
                collection="semantic",
                key="seat-preference",
                value={
                    "subject": "Alice",
                    "predicate": "prefers",
                    "object": "window seats",
                    "context": "air travel",
                },
                entities=("Alice", "flight"),
            ),
            MemoryCandidate(
                collection="semantic",
                key="hotel-preference",
                value={
                    "subject": "Alice",
                    "predicate": "prefers",
                    "object": "Riverside Inn",
                    "context": "trips to Austin",
                },
                entities=("Alice", "Riverside Inn", "Austin"),
            ),
            MemoryCandidate(
                collection="episodes",
                key="seat-correction-request",
                value={
                    "observation": "Alice was assigned an aisle seat and requested a change.",
                    "thoughts": "Her current seat preference should override the old booking.",
                    "action": "Checked availability for a window seat.",
                    "result": "A window seat was available for the next-day change.",
                },
                occurred_at="2025-04-10T09:00:00Z",
                entities=("Alice", "flight"),
            ),
            MemoryCandidate(
                collection="episodes",
                key="seat-correction-completed",
                value={
                    "observation": "The requested window seat became available.",
                    "thoughts": "Applying the known preference would avoid another correction.",
                    "action": "Changed Alice's reservation to the window seat.",
                    "result": "Alice accepted the updated reservation.",
                },
                occurred_at="2025-04-11T10:30:00Z",
                entities=("Alice", "flight"),
            ),
            MemoryCandidate(
                collection="profiles",
                key="alice-travel-profile",
                value={
                    "name": "Alice travel profile",
                    "summary": (
                        "Prioritize window seats and consider Riverside Inn for Austin trips."
                    ),
                    "principles": [
                        "Check current preferences before booking.",
                        "Use successful corrections as future examples.",
                    ],
                    "source_keys": [
                        "semantic/seat-preference",
                        "semantic/hotel-preference",
                        "episodes/seat-correction-completed",
                    ],
                },
                entities=("Alice",),
            ),
        ),
        links=(
            MemoryLinkCandidate(
                source_collection="semantic",
                source_key="seat-preference",
                target_collection="episodes",
                target_key="seat-correction-completed",
                relation="demonstrated_by",
            ),
            MemoryLinkCandidate(
                source_collection="episodes",
                source_key="seat-correction-completed",
                target_collection="profiles",
                target_key="alice-travel-profile",
                relation="supports",
            ),
            MemoryLinkCandidate(
                source_collection="semantic",
                source_key="hotel-preference",
                target_collection="profiles",
                target_key="alice-travel-profile",
                relation="supports",
            ),
        ),
    )


def answer_demo_question(agent: LangMemAgent) -> str:
    """Have the agent combine four retrieval modes into one response."""

    return agent.answer(
        "How should I plan Alice's next trip, and what worked last time?",
        query="Alice travel window Riverside preference",
        start="2025-04-10T00:00:00Z",
        end="2025-04-11T23:59:59Z",
        graph_collection="semantic",
        graph_key="seat-preference",
        profile_collection="profiles",
        profile_key="alice-travel-profile",
    )


def main() -> None:
    """Run the complete deterministic LangMem-inspired agent workflow."""

    agent = build_demo_agent()
    teach_demo_agent(agent)
    answer = answer_demo_question(agent)
    print_agent_demo(
        title="LangMem-inspired namespaced memory",
        purpose=(
            "An agent manages structured values by namespace and retrieves "
            "direct, temporal, and graph context."
        ),
        structures=("keyed collection", "temporal collection", "property graph"),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
