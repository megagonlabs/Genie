"""Run an agent with temporal knowledge graph tools.

From the repository root:

    conda run -n "genie" python -m lamps.graphiti.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.graphiti import (
    FactCandidate,
    Observation,
    TemporalGraphAgent,
    TemporalGraphTools,
    TemporalKnowledgeGraph,
)


def main() -> None:
    """Let an agent learn source material and answer through graph tools."""

    memory = Memory(backend=InMemoryBackend(), principal="graphiti-demo")
    root = memory.put(
        type="temporal_knowledge_graph",
        value={"name": "Customer context"},
    )
    graph = TemporalKnowledgeGraph(memory.get_handle(root=root))
    agent = TemporalGraphAgent(
        name="context-agent",
        tools=TemporalGraphTools(graph),
    )

    agent.learn(
        (
            Observation(
                content="Alice said she lives in San Francisco and works at Acme.",
                source="chat/2024-01-10",
                observed_at="2024-01-10T00:00:00Z",
                facts=(
                    FactCandidate(
                        subject="Alice",
                        subject_type="person",
                        relation="lives_in",
                        object="San Francisco",
                        object_type="city",
                        valid_from="2024-01-10T00:00:00Z",
                        exclusive=True,
                    ),
                    FactCandidate(
                        subject="Alice",
                        subject_type="person",
                        relation="works_at",
                        object="Acme",
                        object_type="company",
                        valid_from="2024-01-10T00:00:00Z",
                    ),
                ),
            ),
            Observation(
                content="Acme is headquartered in San Francisco; Alice and Bob work there.",
                source="directory/2024-02-01",
                observed_at="2024-02-01T00:00:00Z",
                facts=(
                    FactCandidate(
                        subject="Alice",
                        subject_type="person",
                        relation="works_at",
                        object="Acme",
                        object_type="company",
                        valid_from="2024-02-01T00:00:00Z",
                    ),
                    FactCandidate(
                        subject="Bob",
                        subject_type="person",
                        relation="works_at",
                        object="Acme",
                        object_type="company",
                        valid_from="2024-02-01T00:00:00Z",
                    ),
                    FactCandidate(
                        subject="Acme",
                        subject_type="company",
                        relation="headquartered_in",
                        object="San Francisco",
                        object_type="city",
                        valid_from="2024-02-01T00:00:00Z",
                        exclusive=True,
                    ),
                ),
            ),
            Observation(
                content="Alice said she moved to Seattle on June 1.",
                source="chat/2025-06-02",
                observed_at="2025-06-02T00:00:00Z",
                facts=(
                    FactCandidate(
                        subject="Alice",
                        subject_type="person",
                        relation="lives_in",
                        object="Seattle",
                        object_type="city",
                        valid_from="2025-06-01T00:00:00Z",
                        exclusive=True,
                    ),
                ),
            ),
        )
    )

    answer = agent.answer(
        "Where does Alice live now, where did she live before, and who works with her?",
        subject="Alice",
        context_entity="Acme",
        as_of="2025-07-01T00:00:00Z",
        history_relation="lives_in",
    )
    print_agent_demo(tools=agent.tools, answer=answer, calls=agent.call_log)


if __name__ == "__main__":
    main()
