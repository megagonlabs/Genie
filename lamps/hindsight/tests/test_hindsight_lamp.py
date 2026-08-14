"""Tests for the Hindsight-inspired memory-bank lamp."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

import pytest

from genie import InMemoryBackend, Memory
from lamps.hindsight import (
    CAUSAL_RELATION,
    MEMORY_UNIT_TYPE,
    MENTAL_MODEL_TYPE,
    SEMANTIC_RELATION,
    TEMPORAL_RELATION,
    HindsightMemoryBank,
    HindsightStructures,
    OCCURRENCE_OF_RELATION,
    OCCURRENCE_TYPE,
)
from lamps.hindsight.demo import (
    answer_demo_question,
    build_demo_agent,
    teach_demo_agent,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def at(value: str) -> datetime:
    """Parse a test date at midnight UTC."""

    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def make_bank() -> HindsightMemoryBank:
    """Create an empty bank rooted in a bounded Genie handle."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="test")
    root = memory.put(type="hindsight.memory_bank", value={"name": "test"})
    handle = memory.get_handle(root=root)
    return HindsightMemoryBank(
        handle,
        structures=HindsightStructures.create(handle, backend),
    )


def test_atomic_memory_unit_preserves_canonical_fields_and_curation_state() -> None:
    """One block stores the lamp's compact atomic memory-unit structure."""

    bank = make_bank()
    unit = bank.retain(
        "Atlas releases on Friday.",
        fact_type="world",
        entities=("Atlas",),
        occurred_dates=(at("2025-03-14"),),
        metadata={"confidence": 0.9},
        tags=("project:atlas",),
        provenance={"source": "docs/release-policy"},
    )

    assert unit.block.type == MEMORY_UNIT_TYPE
    assert unit.block.value == {
        "text": "Atlas releases on Friday.",
        "fact_type": "world",
        "state": "valid",
        "entities": ["Atlas"],
        "occurred_dates": ["2025-03-14T00:00:00Z"],
        "metadata": {"confidence": 0.9},
        "tags": ["project:atlas"],
        "provenance": {"source": "docs/release-policy"},
    }

    invalidated = bank.set_state(unit, "invalidated")

    assert invalidated.state == "invalidated"
    assert bank.recall("Atlas releases") == []
    assert bank.recall("Atlas releases", include_invalidated=True)[0].unit.id == unit.id


def test_temporal_recall_filters_occurred_dates_and_fact_types() -> None:
    """Temporal retrieval returns active experiences inside the requested range."""

    bank = make_bank()
    before = bank.retain(
        "I prepared the Atlas release.",
        fact_type="experience",
        entities=("Atlas",),
        occurred_dates=(at("2025-03-14"),),
    )
    failure = bank.retain(
        "I deployed Atlas and the migration timed out.",
        fact_type="experience",
        entities=("Atlas",),
        occurred_dates=(at("2025-03-15"),),
    )
    recovery = bank.retain(
        "I retried Atlas and the deployment succeeded.",
        fact_type="experience",
        entities=("Atlas",),
        occurred_dates=(at("2025-03-16"),),
    )
    bank.retain(
        "Atlas migrations benefit from small batches.",
        fact_type="observation",
        entities=("Atlas",),
        occurred_dates=(at("2025-03-16"),),
    )

    results = bank.recall_temporal(
        at("2025-03-15"),
        at("2025-03-16"),
        entity="Atlas",
        fact_types=("experience",),
    )

    assert [unit.id for unit in results] == [failure.id, recovery.id]
    assert before.id not in {unit.id for unit in results}


def test_multiple_occurred_dates_use_child_occurrences_and_deduplicate_recall() -> None:
    """Each date is indexed independently without changing the canonical unit."""

    bank = make_bank()
    unit = bank.retain(
        "The migration ran on two days.",
        fact_type="experience",
        occurred_dates=(at("2025-03-15"), at("2025-03-16")),
    )

    occurrences = bank.memory.search(types=[OCCURRENCE_TYPE], limit=10)
    recalled = bank.recall_temporal(
        at("2025-03-15"),
        at("2025-03-16"),
        fact_types=("experience",),
    )

    assert len(occurrences) == 2
    assert [result.block.parent_id for result in occurrences] == [unit.id, unit.id]
    assert all(
        bank.structures.timeline.get(result.block) is not None
        for result in occurrences
    )
    assert all(
        bank.structures.graph.edges(
            result.block,
            relation=OCCURRENCE_OF_RELATION,
        )[0].target_id
        == unit.id
        for result in occurrences
    )
    assert recalled == [unit]


def test_graph_recall_traverses_temporal_semantic_and_causal_links() -> None:
    """Typed Genie links provide the lamp's three graph pathways."""

    bank = make_bank()
    cause = bank.retain("The migration used one large batch.", fact_type="world")
    failure = bank.retain("The migration timed out.", fact_type="experience")
    recovery = bank.retain("The small-batch retry succeeded.", fact_type="experience")
    observation = bank.retain(
        "Small batches are more reliable.",
        fact_type="observation",
        provenance={"source_memory_ids": [failure.id, recovery.id]},
    )
    bank.link_units(cause, failure, CAUSAL_RELATION)
    bank.link_units(failure, recovery, TEMPORAL_RELATION)
    bank.link_units(observation, failure, SEMANTIC_RELATION)

    results = bank.recall_graph(failure)

    assert [(result.relation, result.direction, result.unit.id) for result in results] == [
        (CAUSAL_RELATION, "in", cause.id),
        (SEMANTIC_RELATION, "in", observation.id),
        (TEMPORAL_RELATION, "out", recovery.id),
    ]


def test_mental_models_are_separate_synthesized_documents() -> None:
    """Mental models are stored separately and retain evidence references."""

    bank = make_bank()
    fact = bank.retain("Atlas releases on Friday.", fact_type="world")
    observation = bank.retain("Small batches reduce failures.", fact_type="observation")

    model = bank.create_mental_model(
        name="atlas-playbook",
        source_query="How should Atlas be deployed?",
        content="Release Friday and use small batches.",
        source_memory_ids=(fact.id, observation.id),
        tags=("project:atlas",),
    )

    assert model.block.type == MENTAL_MODEL_TYPE
    assert model.source_memory_ids == (fact.id, observation.id)
    assert bank.get_mental_model("atlas-playbook") == model
    assert model.id not in {result.unit.id for result in bank.recall("Release Friday")}


def test_validation_keeps_the_lamp_model_explicit() -> None:
    """Taxonomy, time, and graph relation errors fail predictably."""

    bank = make_bank()
    assert bank.get_unit("missing") is None
    with pytest.raises(ValueError, match="fact_type"):
        bank.retain("bad", fact_type=cast(Any, "opinion"))
    with pytest.raises(ValueError, match="timezone-aware"):
        bank.retain(
            "bad time",
            fact_type="world",
            occurred_dates=(datetime(2025, 1, 1),),
        )
    first = bank.retain("first", fact_type="world")
    second = bank.retain("second", fact_type="world")
    with pytest.raises(ValueError, match="Unsupported"):
        bank.link_units(first, second, "hindsight.unknown")


def test_agent_learns_and_answers_only_through_memory_tools() -> None:
    """The demo agent combines atomic, temporal, graph, and model retrieval."""

    agent = build_demo_agent()
    assert [tool.name for tool in agent.tools] == [
        "retain",
        "link_memories",
        "create_mental_model",
        "recall",
        "recall_temporal",
        "recall_graph",
        "get_mental_model",
    ]
    assert not hasattr(agent, "_bank")

    teach_demo_agent(agent)
    answer = answer_demo_question(agent)

    assert "Mental model (atlas-deployment-playbook)" in answer
    assert "[observation] Atlas database migrations are more reliable" in answer
    assert "2025-03-15: I deployed Atlas 2.0" in answer
    assert "causal (in): Atlas 2.0 migrated all customer records" in answer
    assert [record.name for record in agent.call_log] == [
        "retain",
        "retain",
        "retain",
        "retain",
        "retain",
        "link_memories",
        "link_memories",
        "link_memories",
        "link_memories",
        "create_mental_model",
        "get_mental_model",
        "recall",
        "recall_temporal",
        "recall_graph",
    ]
    observation_call = agent.call_log[4]
    assert observation_call.result["provenance"]["source_memory_ids"] == [
        agent.memory_id("timeout"),
        agent.memory_id("retry"),
    ]
    assert agent.tool_usage() == (
        "retain x5, link_memories x4, create_mental_model x1, "
        "get_mental_model x1, recall x1, recall_temporal x1, recall_graph x1"
    )


def test_demo_runs_as_a_module() -> None:
    """The lamp is runnable from the repository root."""

    result = subprocess.run(
        [sys.executable, "-m", "lamps.hindsight"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("== Hindsight-inspired agent memory ==\n")
    assert "Genie structures: event log, keyed collection, temporal collection" in result.stdout
    assert "Tools available to the agent:" in result.stdout
    assert "Agent answer:" in result.stdout
    assert "Mental model (atlas-deployment-playbook)" in result.stdout
    assert "Experience timeline for Atlas:" in result.stdout
    assert "Graph context around timeout:" in result.stdout
    assert "Inspectable tool calls:\n01. retain\n02. retain" in result.stdout
    assert "Tool usage:\n- retain x5\n- link_memories x4" in result.stdout
