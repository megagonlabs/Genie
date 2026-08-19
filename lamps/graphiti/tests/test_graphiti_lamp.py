"""Tests for the Graphiti-inspired temporal knowledge graph lamp."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from genie import InMemoryBackend, Memory
from lamps.graphiti import (
    FactCandidate,
    GraphitiStructures,
    Observation,
    TemporalGraphAgent,
    TemporalGraphTools,
    TemporalKnowledgeGraph,
)
from lamps.graphiti.temporal_graph import SUPERSEDED_BY_RELATION

REPO_ROOT = Path(__file__).resolve().parents[3]


def at(value: str) -> datetime:
    """Parse a test date at midnight UTC."""

    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def make_graph() -> TemporalKnowledgeGraph:
    """Create an empty graph rooted in a bounded Genie handle."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="test")
    root = memory.put(type="temporal_knowledge_graph", value={"name": "test"})
    handle = memory.get_handle(root=root)
    return TemporalKnowledgeGraph(
        handle,
        structures=GraphitiStructures.create(handle, backend),
    )


def make_agent() -> TemporalGraphAgent:
    """Create an agent that receives only temporal graph tools."""

    return TemporalGraphAgent(
        name="test-agent",
        tools=TemporalGraphTools(make_graph()),
    )


def test_exclusive_fact_preserves_history_and_provenance() -> None:
    """A new exclusive fact closes, rather than deletes, the old fact."""

    graph = make_graph()
    alice = graph.add_entity("Alice", entity_type="person")
    san_francisco = graph.add_entity("San Francisco", entity_type="city")
    seattle = graph.add_entity("Seattle", entity_type="city")
    first_episode = graph.add_episode(
        "Alice lives in San Francisco.",
        source="chat/1",
        observed_at=at("2024-01-10"),
    )
    moving_episode = graph.add_episode(
        "Alice moved to Seattle.",
        source="chat/2",
        observed_at=at("2025-06-02"),
    )

    first = graph.assert_fact(
        alice,
        "lives_in",
        san_francisco,
        episode=first_episode,
        valid_from=at("2024-01-10"),
        exclusive=True,
    )
    second = graph.assert_fact(
        alice,
        "lives_in",
        seattle,
        episode=moving_episode,
        valid_from=at("2025-06-01"),
        exclusive=True,
    )

    before = graph.facts_at(at("2025-05-31"), subject=alice, relation="lives_in")
    after = graph.facts_at(at("2025-06-01"), subject=alice, relation="lives_in")
    history = graph.history(alice, relation="lives_in")

    assert [fact.object.value["name"] for fact in before] == ["San Francisco"]
    assert [fact.object.value["name"] for fact in after] == ["Seattle"]
    assert history[0].valid_to == at("2025-06-01")
    assert history[1].valid_to is None
    assert graph.structures.timeline.get(history[0].block) is not None
    assert [episode.value["source"] for episode in graph.provenance(history[0])] == [
        "chat/1",
        "chat/2",
    ]
    assert [episode.value["source"] for episode in graph.provenance(history[1])] == [
        "chat/2"
    ]
    assert graph.memory.list_links(
        first.block,
        relation=SUPERSEDED_BY_RELATION,
    )[0].target_id == second.block.id


def test_reasserting_same_fact_adds_provenance_without_duplication() -> None:
    """Repeated evidence is consolidated onto the active fact."""

    graph = make_graph()
    alice = graph.add_entity("Alice", entity_type="person")
    seattle = graph.add_entity("Seattle", entity_type="city")
    first_episode = graph.add_episode(
        "Alice lives in Seattle.",
        source="chat/1",
        observed_at=at("2025-06-01"),
    )
    second_episode = graph.add_episode(
        "Alice confirmed she lives in Seattle.",
        source="chat/2",
        observed_at=at("2025-06-02"),
    )
    first = graph.assert_fact(
        alice,
        "lives_in",
        seattle,
        episode=first_episode,
        valid_from=at("2025-06-01"),
        exclusive=True,
    )

    repeated = graph.assert_fact(
        alice,
        "lives_in",
        seattle,
        episode=second_episode,
        valid_from=at("2025-06-02"),
        exclusive=True,
    )

    assert repeated.block.id == first.block.id
    assert len(graph.history(alice, relation="lives_in")) == 1
    assert [episode.value["source"] for episode in graph.provenance(repeated)] == [
        "chat/1",
        "chat/2",
    ]


def test_neighborhood_query_includes_incoming_and_outgoing_facts() -> None:
    """A temporal neighborhood exposes both directions around an entity."""

    graph = make_graph()
    alice = graph.add_entity("Alice", entity_type="person")
    acme = graph.add_entity("Acme", entity_type="company")
    san_francisco = graph.add_entity("San Francisco", entity_type="city")
    episode = graph.add_episode(
        "Alice works at Acme, which is headquartered in San Francisco.",
        source="directory/1",
        observed_at=at("2024-02-01"),
    )
    graph.assert_fact(
        alice,
        "works_at",
        acme,
        episode=episode,
        valid_from=at("2024-02-01"),
    )
    graph.assert_fact(
        acme,
        "headquartered_in",
        san_francisco,
        episode=episode,
        valid_from=at("2024-02-01"),
    )

    neighborhood = graph.neighborhood_at(acme, at("2025-01-01"))

    assert [fact.statement for fact in neighborhood] == [
        "Acme -[headquartered_in]-> San Francisco",
        "Alice -[works_at]-> Acme",
    ]


def test_temporal_validation_is_explicit() -> None:
    """Naive, reversed, and out-of-order effective times are rejected."""

    graph = make_graph()
    alice = graph.add_entity("Alice", entity_type="person")
    seattle = graph.add_entity("Seattle", entity_type="city")
    portland = graph.add_entity("Portland", entity_type="city")
    episode = graph.add_episode(
        "Alice lives in Seattle.",
        source="chat/1",
        observed_at=at("2025-06-01"),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        graph.assert_fact(
            alice,
            "lives_in",
            seattle,
            episode=episode,
            valid_from=datetime(2025, 6, 1),
        )
    with pytest.raises(ValueError, match="later than"):
        graph.assert_fact(
            alice,
            "lives_in",
            seattle,
            episode=episode,
            valid_from=at("2025-06-01"),
            valid_to=at("2025-05-31"),
        )

    graph.assert_fact(
        alice,
        "lives_in",
        seattle,
        episode=episode,
        valid_from=at("2025-06-01"),
        exclusive=True,
    )
    with pytest.raises(ValueError, match="effective-time order"):
        graph.assert_fact(
            alice,
            "lives_in",
            portland,
            episode=episode,
            valid_from=at("2025-05-01"),
            exclusive=True,
        )


def test_agent_learns_and_answers_only_through_tools() -> None:
    """The example agent writes and queries the graph through named tools."""

    agent = make_agent()
    assert [tool.name for tool in agent.tools] == [
        "add_episode",
        "add_fact",
        "query_entity",
        "query_neighborhood",
        "get_history",
    ]

    agent.learn(
        (
            Observation(
                content="Alice lives in San Francisco and works at Acme.",
                source="chat/1",
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
                content="Alice moved to Seattle.",
                source="chat/2",
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
        "Where does Alice live now and where did she live before?",
        subject="Alice",
        context_entity="Acme",
        as_of="2025-07-01T00:00:00Z",
        history_relation="lives_in",
    )

    assert "Alice -[lives_in]-> Seattle" in answer
    assert "Alice -[lives_in]-> San Francisco" in answer
    assert [record.name for record in agent.call_log] == [
        "add_episode",
        "add_fact",
        "add_fact",
        "add_episode",
        "add_fact",
        "query_entity",
        "query_neighborhood",
        "get_history",
    ]
    assert agent.tool_usage() == (
        "add_episode x2, add_fact x3, query_entity x1, "
        "query_neighborhood x1, get_history x1"
    )


def test_demo_runs_as_a_module() -> None:
    """The lamp is runnable from the repository root."""

    result = subprocess.run(
        [sys.executable, "-m", "lamps.graphiti"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("== Graphiti-inspired temporal knowledge graph ==\n")
    assert "Genie structures: property graph, temporal collection, lineage DAG" in result.stdout
    assert "Tools available to the agent:" in result.stdout
    assert "- add_episode: Store raw source material for later provenance." in result.stdout
    assert "Agent answer:" in result.stdout
    assert "Alice -[lives_in]-> Seattle" in result.stdout
    assert "Alice -[lives_in]-> San Francisco" in result.stdout
    assert "Related context around Acme:" in result.stdout
    assert "Inspectable tool calls:\n01. add_episode\n02. add_fact" in result.stdout
    assert "Tool usage:\n- add_episode x3\n- add_fact x6" in result.stdout
