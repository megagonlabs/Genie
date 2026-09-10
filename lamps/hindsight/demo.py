"""Run an agent with Hindsight-inspired memory tools.

From the repository root:

    python -m lamps.hindsight.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.hindsight.agent import (
    DocumentCandidate,
    EntityCandidate,
    FactCandidate,
    HindsightAgent,
    HindsightTools,
    LinkCandidate,
    ModelCandidate,
)
from lamps.hindsight.bank import HindsightMemoryBank
from lamps.hindsight.mental_models import MentalModelStore
from lamps.hindsight.model import (
    BANK_TYPE,
    CAUSAL_RELATION,
    SEMANTIC_RELATION,
    TEMPORAL_RELATION,
)
from lamps.hindsight.observations import ConsolidationEngine
from lamps.hindsight.recall import HindsightRecall
from lamps.hindsight.reflect import Reflector
from lamps.hindsight.structures import HindsightStructures

ENTITIES = (
    EntityCandidate(name="Alice Chen", kind="person", aliases=("Alice",)),
    EntityCandidate(name="Northwind billing", kind="service", aliases=("billing service",)),
)

MODELS = (
    ModelCandidate(
        name="northwind-change-brief",
        question="What should I know before changing Northwind?",
        tags=("northwind",),
    ),
)

DOCUMENTS = (
    DocumentCandidate(
        source="chat/2025-01-14",
        content=(
            "Alice Chen: we build every Northwind frontend in React, and the "
            "billing service stores its data in MySQL."
        ),
        context="onboarding call with the platform team",
        retained_at="2025-01-14T09:00:00Z",
        facts=(
            FactCandidate(
                key="react",
                text="Alice Chen's team builds every Northwind frontend in React.",
                fact_type="world",
                context="stated on the onboarding call",
                entities=("Alice Chen", "React"),
                tags=("northwind", "frontend"),
                claim="frontend-stack",
                belief="Northwind frontends are built in React.",
            ),
            FactCandidate(
                key="mysql",
                text="The Northwind billing service stores its data in MySQL.",
                fact_type="world",
                context="stated on the onboarding call",
                entities=("Northwind billing", "MySQL"),
                tags=("northwind", "billing"),
            ),
        ),
    ),
    DocumentCandidate(
        source="agent-run/2025-02-03",
        content=(
            "I migrated Northwind billing to the new schema in a single batch. "
            "The migration timed out after forty minutes."
        ),
        context="release automation run",
        retained_at="2025-02-03T18:20:00Z",
        facts=(
            FactCandidate(
                key="timeout",
                text=(
                    "I migrated Northwind billing in one batch on February 3 and "
                    "the migration timed out."
                ),
                fact_type="experience",
                context="release automation run",
                entities=("Northwind billing",),
                occurred_at="2025-02-03T17:40:00Z",
                occurred_until="2025-02-03T18:20:00Z",
                tags=("northwind", "billing", "migration"),
                claim="migration-batching",
                belief="Northwind billing migrations finish only in small batches.",
            ),
        ),
    ),
    DocumentCandidate(
        source="agent-run/2025-03-09",
        content=(
            "I retried the Northwind billing migration in four small batches. "
            "It finished in six minutes."
        ),
        context="release automation run",
        retained_at="2025-03-09T11:05:00Z",
        facts=(
            FactCandidate(
                key="retry",
                text=(
                    "I retried the Northwind billing migration on March 9 in four "
                    "small batches and it succeeded."
                ),
                fact_type="experience",
                context="release automation run",
                entities=("Northwind billing",),
                occurred_at="2025-03-09T10:59:00Z",
                occurred_until="2025-03-09T11:05:00Z",
                tags=("northwind", "billing", "migration"),
                claim="migration-batching",
                belief="Northwind billing migrations finish only in small batches.",
            ),
        ),
        links=(
            LinkCandidate(
                source_key="timeout",
                target_key="retry",
                relation=TEMPORAL_RELATION,
                metadata={"sequence": "next attempt"},
            ),
            LinkCandidate(
                source_key="timeout",
                target_key="retry",
                relation=CAUSAL_RELATION,
                metadata={"reason": "the timeout is why the retry used small batches"},
            ),
        ),
    ),
    DocumentCandidate(
        source="chat/2025-06-15",
        content=(
            "Alice C.: we moved the whole team off React to Vue in June. Billing "
            "has actually been on Postgres since 2024, so the MySQL note was wrong."
        ),
        context="quarterly planning call",
        retained_at="2025-06-15T14:30:00Z",
        facts=(
            FactCandidate(
                key="vue",
                text="Alice Chen's team builds every Northwind frontend in Vue.",
                fact_type="world",
                context="stated on the quarterly planning call",
                entities=("Alice C.", "Vue"),
                occurred_at="2025-06-01T00:00:00Z",
                tags=("northwind", "frontend"),
                claim="frontend-stack",
                belief="Northwind frontends are built in Vue.",
                stance="contradicts",
            ),
            FactCandidate(
                key="postgres",
                text="The Northwind billing service stores its data in Postgres.",
                fact_type="world",
                context="correction on the quarterly planning call",
                entities=("Northwind billing", "Postgres"),
                occurred_at="2024-01-01T00:00:00Z",
                tags=("northwind", "billing"),
            ),
        ),
        links=(
            LinkCandidate(
                source_key="vue",
                target_key="react",
                relation=SEMANTIC_RELATION,
                metadata={"role": "replaces"},
            ),
        ),
        corrections=("mysql",),
    ),
)

QUESTION = "What should I know before I change Northwind billing?"
QUERY = "Northwind billing migration batches"
WINDOW_START = "2025-03-01T00:00:00Z"
WINDOW_END = "2025-04-01T00:00:00Z"


def build_agent() -> HindsightAgent:
    """Assemble an empty bank, its tool set, and an agent over them.

    Returns:
        An agent whose only memory access is the named tool set.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="hindsight-demo")
    root = memory.put(type=BANK_TYPE, value={"name": "Northwind platform"})
    handle = memory.get_handle(root=root)
    bank = HindsightMemoryBank(
        handle,
        structures=HindsightStructures.create(handle, backend),
    )
    consolidation = ConsolidationEngine(bank)
    models = MentalModelStore(bank, consolidation)
    recall = HindsightRecall(bank, consolidation)
    return HindsightAgent(
        name="platform-agent",
        tools=HindsightTools(
            bank,
            consolidation=consolidation,
            models=models,
            recall=recall,
            reflector=Reflector(bank, models, recall),
        ),
    )


def main() -> None:
    """Let an agent retain, consolidate, and answer through memory tools."""

    agent = build_agent()
    agent.learn(DOCUMENTS, entities=ENTITIES, models=MODELS)
    answer = agent.answer(
        QUESTION,
        model_name="northwind-change-brief",
        query=QUERY,
        entity="billing service",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    print_agent_demo(
        title="Hindsight-inspired agent memory",
        purpose=(
            "An agent retains documents, consolidates their facts into "
            "versioned beliefs, keeps a standing answer current, and reflects "
            "from the highest layer that already knows."
        ),
        structures=(
            "ownership tree",
            "event log",
            "temporal collection",
            "keyed collection",
            "property graph",
            "two versioned records",
            "lineage DAG",
        ),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
