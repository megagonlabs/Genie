"""Run an agent with Hindsight-inspired memory tools.

From the repository root:

    conda run -n "genie" python -m lamps.hindsight.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.hindsight.agent import (
    HindsightAgent,
    HindsightTools,
    MemoryCandidate,
    MemoryLinkCandidate,
    MentalModelCandidate,
)
from lamps.hindsight.memory_bank import (
    CAUSAL_RELATION,
    SEMANTIC_RELATION,
    TEMPORAL_RELATION,
    HindsightMemoryBank,
    HindsightStructures,
)


def build_demo_agent() -> HindsightAgent:
    """Create an agent that receives only bounded memory tools."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="hindsight-demo")
    root = memory.put(
        type="hindsight.memory_bank",
        value={"name": "Atlas operations"},
    )
    handle = memory.get_handle(root=root)
    bank = HindsightMemoryBank(
        handle,
        structures=HindsightStructures.create(handle, backend),
    )
    return HindsightAgent(name="operations-agent", tools=HindsightTools(bank))


def teach_demo_agent(agent: HindsightAgent) -> None:
    """Have the agent retain facts, experiences, links, and a mental model."""

    agent.learn(
        memories=(
            MemoryCandidate(
                key="release_policy",
                text="Atlas production releases are scheduled for Fridays.",
                fact_type="world",
                entities=("Atlas",),
                tags=("project:atlas", "deployment"),
                provenance={"source": "docs/release-policy"},
            ),
            MemoryCandidate(
                key="large_batch",
                text="Atlas 2.0 migrated all customer records in one large batch.",
                fact_type="world",
                entities=("Atlas", "Atlas database"),
                occurred_dates=("2025-03-15T09:00:00Z",),
                tags=("project:atlas", "deployment"),
                provenance={"source": "release/atlas-2.0"},
            ),
            MemoryCandidate(
                key="timeout",
                text="I deployed Atlas 2.0 on March 15; the database migration timed out.",
                fact_type="experience",
                entities=("Atlas", "Atlas database"),
                occurred_dates=("2025-03-15T09:30:00Z",),
                metadata={"outcome": "failed"},
                tags=("project:atlas", "deployment"),
                provenance={"source": "agent-run/17"},
            ),
            MemoryCandidate(
                key="retry",
                text=(
                    "I retried Atlas 2.0 on March 16 using smaller migration "
                    "batches; the deployment succeeded."
                ),
                fact_type="experience",
                entities=("Atlas", "Atlas database"),
                occurred_dates=("2025-03-16T10:00:00Z",),
                metadata={"outcome": "succeeded"},
                tags=("project:atlas", "deployment"),
                provenance={"source": "agent-run/18"},
            ),
            MemoryCandidate(
                key="batch_observation",
                text="Atlas database migrations are more reliable when run in small batches.",
                fact_type="observation",
                entities=("Atlas", "Atlas database"),
                occurred_dates=(
                    "2025-03-15T09:30:00Z",
                    "2025-03-16T10:00:00Z",
                ),
                tags=("project:atlas", "deployment"),
                provenance={"method": "agent synthesis"},
                derived_from=("timeout", "retry"),
            ),
        ),
        links=(
            MemoryLinkCandidate(
                source_key="large_batch",
                target_key="timeout",
                relation=CAUSAL_RELATION,
                metadata={"reason": "large migration batch preceded the timeout"},
            ),
            MemoryLinkCandidate(
                source_key="timeout",
                target_key="retry",
                relation=TEMPORAL_RELATION,
                metadata={"sequence": "next attempt"},
            ),
            MemoryLinkCandidate(
                source_key="batch_observation",
                target_key="timeout",
                relation=SEMANTIC_RELATION,
                metadata={"role": "supporting evidence"},
            ),
            MemoryLinkCandidate(
                source_key="batch_observation",
                target_key="retry",
                relation=SEMANTIC_RELATION,
                metadata={"role": "supporting evidence"},
            ),
        ),
        mental_model=MentalModelCandidate(
            name="atlas-deployment-playbook",
            source_query="How should I deploy Atlas safely?",
            content=(
                "Deploy Atlas on Friday, use small database-migration batches, "
                "and retry failed batches separately."
            ),
            source_keys=("release_policy", "batch_observation", "retry"),
            tags=("project:atlas", "deployment"),
        ),
    )


def answer_demo_question(agent: HindsightAgent) -> str:
    """Have the agent combine four retrieval paths into one answer."""

    return agent.answer(
        "How should I deploy Atlas, and what past evidence supports that plan?",
        query="Atlas deployment migration batches",
        entity="Atlas",
        start="2025-03-15T00:00:00Z",
        end="2025-03-16T23:59:59Z",
        graph_anchor="timeout",
        mental_model_name="atlas-deployment-playbook",
    )


def main() -> None:
    """Run the deterministic Hindsight-inspired agent workflow."""

    agent = build_demo_agent()
    teach_demo_agent(agent)
    answer = answer_demo_question(agent)
    print_agent_demo(
        title="Hindsight-inspired agent memory",
        purpose=(
            "An agent combines atomic memories, dated experiences, graph "
            "relations, and a synthesized mental model."
        ),
        structures=(
            "event log",
            "keyed collection",
            "temporal collection",
            "property graph",
        ),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
