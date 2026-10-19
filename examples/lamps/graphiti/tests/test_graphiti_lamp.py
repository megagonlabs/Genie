"""Tests for the Graphiti-inspired lamp, a bi-temporal knowledge graph."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from examples.lamps.graphiti import (
    GRAPH_TYPE,
    RECIPE_NAME,
    CommunityBuilder,
    EdgeType,
    EntityType,
    GraphitiSearch,
    GraphitiStructures,
    Ontology,
    SearchFilters,
    TemporalGraphAgent,
    TemporalGraphTools,
    TemporalKnowledgeGraph,
)
from examples.lamps.graphiti.demo import AS_OF, KNOWN_AT, OBSERVATIONS, build_agent
from examples.lamps.graphiti.model import SUPERSEDED_BY_RELATION
from genie import InMemoryBackend, Memory

REPO_ROOT = Path(__file__).resolve().parents[4]

ONTOLOGY = Ontology.prescribe(
    entities=[
        EntityType(name="person", description="A human."),
        EntityType(name="company", description="An employer."),
        EntityType(name="city", description="A place."),
    ],
    edges=[
        EdgeType(
            name="lives_in",
            description="Where a person lives.",
            subject_types=frozenset({"person"}),
            object_types=frozenset({"city"}),
            single_valued=True,
        ),
        EdgeType(
            name="works_at",
            description="Where a person works.",
            subject_types=frozenset({"person"}),
            object_types=frozenset({"company"}),
            single_valued=True,
        ),
        EdgeType(
            name="headquartered_in",
            description="Where a company sits.",
            subject_types=frozenset({"company"}),
            object_types=frozenset({"city"}),
            single_valued=True,
        ),
        EdgeType(
            name="manages",
            description="Who a person reports to.",
            subject_types=frozenset({"person"}),
            object_types=frozenset({"person"}),
        ),
    ],
)


def at(value: str) -> datetime:
    """Parse a test date at midnight UTC."""

    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def make_graph(*, ontology: Ontology = ONTOLOGY) -> TemporalKnowledgeGraph:
    """Create an empty graph rooted in a bounded Genie handle."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="test")
    root = memory.put(type=GRAPH_TYPE, value={"name": "test"})
    handle = memory.get_handle(root=root)
    return TemporalKnowledgeGraph(
        handle,
        structures=GraphitiStructures.create(handle, backend),
        ontology=ontology,
    )


def populate(graph: TemporalKnowledgeGraph) -> None:
    """Add the shared scenario of 4 episodes and their facts that several tests use."""

    first = graph.add_episode(
        name="chat/1",
        content="Alice lives in San Francisco and works at Acme.",
        reference_time=at("2024-01-10"),
        ingested_at=at("2024-01-10"),
        source="message",
    )
    graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="San Francisco",
        object_type="city",
        episode=first,
        valid_at=at("2024-01-10"),
    )
    graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="works_at",
        object="Acme",
        object_type="company",
        episode=first,
        valid_at=at("2024-01-10"),
    )

    directory = graph.add_episode(
        name="directory/1",
        content="Acme is headquartered in San Francisco and Bob works there.",
        reference_time=at("2024-02-01"),
        ingested_at=at("2024-02-01"),
    )
    graph.add_triplet(
        subject="Acme",
        subject_type="company",
        relation="headquartered_in",
        object="San Francisco",
        object_type="city",
        episode=directory,
        valid_at=at("2024-02-01"),
    )
    graph.add_triplet(
        subject="Bob",
        subject_type="person",
        relation="works_at",
        object="Acme",
        object_type="company",
        episode=directory,
        valid_at=at("2024-02-01"),
    )

    moved = graph.add_episode(
        name="chat/2",
        content="Alice moved to Seattle on June 1.",
        reference_time=at("2025-06-02"),
        ingested_at=at("2025-06-20"),
        source="message",
    )
    graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="Seattle",
        object_type="city",
        episode=moved,
        valid_at=at("2025-06-01"),
    )

    hired = graph.add_episode(
        name="news/1",
        content="Cascade Analytics in Seattle hired Alice; Dana already worked there.",
        reference_time=at("2025-07-05"),
        ingested_at=at("2025-07-05"),
    )
    graph.add_triplet(
        subject="Cascade Analytics",
        subject_type="company",
        relation="headquartered_in",
        object="Seattle",
        object_type="city",
        episode=hired,
        valid_at=at("2025-06-25"),
    )
    graph.add_triplet(
        subject="Dana",
        subject_type="person",
        relation="works_at",
        object="Cascade Analytics",
        object_type="company",
        episode=hired,
        valid_at=at("2025-06-25"),
    )
    graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="works_at",
        object="Cascade Analytics",
        object_type="company",
        episode=hired,
        valid_at=at("2025-07-01"),
    )


def test_prescribed_ontology_rejects_undeclared_types_and_endpoints() -> None:
    """A prescribed ontology rejects an undeclared entity type, an undeclared relation, and an endpoint that the relation does not connect."""

    graph = make_graph()
    episode = graph.add_episode(
        name="chat/1",
        content="Alice works at Acme.",
        reference_time=at("2024-01-10"),
        ingested_at=at("2024-01-10"),
    )

    with pytest.raises(ValueError, match="not in the prescribed ontology"):
        graph.resolve_entity("Rover", entity_type="dog")
    with pytest.raises(ValueError, match="not in the prescribed ontology"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="enjoys",
            object="Acme",
            object_type="company",
            episode=episode,
            valid_at=at("2024-01-10"),
        )
    with pytest.raises(ValueError, match="does not connect"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="lives_in",
            object="Acme",
            object_type="company",
            episode=episode,
            valid_at=at("2024-01-10"),
        )


def test_single_valued_relation_closes_both_time_axes() -> None:
    """For a single-valued relation, a contradiction invalidates the old fact on both time axes and does not delete it."""

    graph = make_graph()
    populate(graph)
    alice = graph.get_entity("Alice")
    assert alice is not None

    history = graph.history(alice, relation="lives_in")

    assert [fact.object.value["name"] for fact in history] == ["San Francisco", "Seattle"]
    assert history[0].valid_at == at("2024-01-10")
    assert history[0].invalid_at == at("2025-06-01")
    assert history[0].ingested_at == at("2024-01-10")
    assert history[0].expired_at == at("2025-06-20")
    assert history[1].invalid_at is None
    assert history[1].expired_at is None

    superseded = graph.superseded_by(history[0])
    assert superseded is not None
    assert superseded.id == history[1].id

    assert [episode.name for episode in graph.provenance(history[0])] == ["chat/1", "chat/2"]
    assert graph.memory.list_links(history[0].block, relation=SUPERSEDED_BY_RELATION)[0].target_id == history[1].id


def test_bitemporal_queries_separate_truth_from_knowledge() -> None:
    """Valid time selects what was true at a moment, and transaction time selects what the graph knew at that moment."""

    graph = make_graph()
    populate(graph)

    true_in_june = graph.facts_at(at("2025-06-10"), relation="lives_in")
    known_in_june = graph.facts_known_at(at("2025-06-10"), relation="lives_in")
    both = graph.facts_at(at("2024-06-01"), known_at=at("2025-01-01"), relation="lives_in")

    assert [fact.object.value["name"] for fact in true_in_june] == ["Seattle"]
    assert [fact.object.value["name"] for fact in known_in_june] == ["San Francisco"]
    assert [fact.object.value["name"] for fact in both] == ["San Francisco"]
    assert graph.facts_at(at("2025-06-10"), known_at=at("2025-08-01"), relation="lives_in")


def test_repeated_triplet_merges_provenance_without_duplicating() -> None:
    """If a triplet is already on record, a second assertion adds its episode as evidence to the same fact."""

    graph = make_graph()
    first = graph.add_episode(
        name="chat/1",
        content="Alice lives in Seattle.",
        reference_time=at("2025-06-01"),
        ingested_at=at("2025-06-01"),
    )
    second = graph.add_episode(
        name="chat/2",
        content="Alice confirmed she lives in Seattle.",
        reference_time=at("2025-06-02"),
        ingested_at=at("2025-06-02"),
    )
    created = graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="Seattle",
        object_type="city",
        episode=first,
        valid_at=at("2025-06-01"),
    )

    repeated = graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="Seattle",
        object_type="city",
        episode=second,
        valid_at=at("2025-06-02"),
    )

    assert created.created is True
    assert repeated.created is False
    assert repeated.fact.id == created.fact.id
    assert repeated.invalidated == ()

    alice = graph.get_entity("Alice")
    assert alice is not None
    assert len(graph.history(alice, relation="lives_in")) == 1
    assert [episode.name for episode in graph.provenance(repeated.fact)] == ["chat/1", "chat/2"]


def test_learned_ontology_keeps_every_fact_active() -> None:
    """Without a declared cardinality, the graph does not invalidate a fact automatically."""

    graph = make_graph(ontology=Ontology.learned())
    episode = graph.add_episode(
        name="notes/1",
        content="Alice enjoys hiking and climbing.",
        reference_time=at("2025-01-01"),
        ingested_at=at("2025-01-01"),
    )
    for hobby in ("hiking", "climbing"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="enjoys",
            object=hobby,
            object_type="activity",
            episode=episode,
            valid_at=at("2025-01-01"),
        )

    active = graph.facts_at(at("2025-02-01"), relation="enjoys")

    assert [fact.object.value["name"] for fact in active] == ["climbing", "hiking"]
    assert all(fact.expired_at is None for fact in active)


def test_entity_summaries_keep_every_revision() -> None:
    """Entity summaries are an ordered log of immutable revisions."""

    graph = make_graph()
    alice = graph.resolve_entity("Alice", entity_type="person")
    graph.add_episode(
        name="chat/1",
        content="Alice moved to Seattle.",
        reference_time=at("2025-06-01"),
        ingested_at=at("2025-06-01"),
    )

    updated = graph.update_entity_summary(alice, "Alice now lives in Seattle.")
    revisions = graph.summary_history(alice)

    assert alice.revision == 1
    assert updated.revision == 2
    assert updated.summary == "Alice now lives in Seattle."
    assert [revision.text for revision in revisions] == [
        "Alice is a person.",
        "Alice now lives in Seattle.",
    ]
    assert revisions[1].recorded_at == at("2025-06-01")


def test_episodes_anchor_provenance_in_both_directions() -> None:
    """An episode mentions entities, and the facts that the episode produced cite it as their source."""

    graph = make_graph()
    populate(graph)
    moved = next(episode for episode in graph.episodes() if episode.name == "chat/2")

    assert [episode.name for episode in graph.episodes()] == [
        "chat/1",
        "directory/1",
        "chat/2",
        "news/1",
    ]
    assert [episode.sequence for episode in graph.episodes()] == [0, 1, 2, 3]
    assert sorted(block.value["name"] for block in graph.mentioned_entities(moved)) == [
        "Alice",
        "Seattle",
    ]
    assert [fact.statement for fact in graph.facts_from_episode(moved)] == [
        "Alice -[lives_in]-> San Francisco",
        "Alice -[lives_in]-> Seattle",
    ]
    assert [episode.name for episode in graph.episodes_between(stop=at("2024-06-01"))] == [
        "chat/1",
        "directory/1",
    ]


def test_time_order_rules_are_explicit() -> None:
    """The graph rejects naive, reversed, and out-of-order timestamps."""

    graph = make_graph()
    episode = graph.add_episode(
        name="chat/1",
        content="Alice lives in Seattle.",
        reference_time=at("2025-06-01"),
        ingested_at=at("2025-06-01"),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="lives_in",
            object="Seattle",
            object_type="city",
            episode=episode,
            valid_at=datetime(2025, 6, 1),  # noqa: DTZ001
        )
    with pytest.raises(ValueError, match="later than"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="lives_in",
            object="Seattle",
            object_type="city",
            episode=episode,
            valid_at=at("2025-06-01"),
            invalid_at=at("2025-05-01"),
        )
    with pytest.raises(ValueError, match="non-decreasing ingestion-time order"):
        graph.add_episode(
            name="chat/0",
            content="Older ingestion.",
            reference_time=at("2024-01-01"),
            ingested_at=at("2024-01-01"),
        )

    graph.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="Seattle",
        object_type="city",
        episode=episode,
        valid_at=at("2025-06-01"),
    )
    later = graph.add_episode(
        name="chat/2",
        content="Alice lived in Portland earlier.",
        reference_time=at("2025-06-05"),
        ingested_at=at("2025-06-05"),
    )
    with pytest.raises(ValueError, match="effective-time order"):
        graph.add_triplet(
            subject="Alice",
            subject_type="person",
            relation="lives_in",
            object="Portland",
            object_type="city",
            episode=later,
            valid_at=at("2025-05-01"),
        )


def test_wrong_block_kinds_are_refused() -> None:
    """The graph checks the application-defined block type of each block that it uses as an episode or a fact."""

    graph = make_graph()
    alice = graph.resolve_entity("Alice", entity_type="person")
    episode = graph.add_episode(
        name="chat/1",
        content="Alice lives in Seattle.",
        reference_time=at("2025-06-01"),
        ingested_at=at("2025-06-01"),
    )

    with pytest.raises(TypeError, match="episode block type"):
        graph.add_triplet(
            subject=alice,
            relation="lives_in",
            object="Seattle",
            object_type="city",
            episode=alice.id,
            valid_at=at("2025-06-01"),
        )
    with pytest.raises(TypeError, match="fact block type"):
        graph.resolve_fact(episode.id)


def test_communities_group_the_entities_a_slice_connects() -> None:
    """Connected components of the valid-fact graph become community nodes."""

    graph = make_graph()
    populate(graph)
    communities = CommunityBuilder(graph)

    built = communities.build(as_of=at("2025-08-01"))

    assert [community.name for community in built] == [
        "Acme community",
        "Cascade Analytics community",
    ]
    assert [community.member_names for community in built] == [
        ("Acme", "Bob", "San Francisco"),
        ("Alice", "Cascade Analytics", "Dana", "Seattle"),
    ]

    dana = graph.get_entity("Dana")
    assert dana is not None
    dana_community = communities.community_of(dana)
    assert dana_community is not None
    assert dana_community.name == "Cascade Analytics community"

    alice = graph.get_entity("Alice")
    assert alice is not None
    alice_community = communities.community_of(alice)
    assert alice_community is not None
    assert "Alice is a person." in alice_community.summary

    assert communities.community_of(graph.resolve_entity("Eve", entity_type="person")) is None


def test_communities_follow_the_slice_they_are_built_from() -> None:
    """A community build at an earlier time gives the communities of that earlier time."""

    graph = make_graph()
    populate(graph)
    communities = CommunityBuilder(graph)

    early = communities.build(as_of=at("2024-06-01"))
    late = communities.build(as_of=at("2025-08-01"))

    assert [community.member_names for community in early] == [
        ("Acme", "Alice", "Bob", "San Francisco"),
    ]
    assert [community.member_names for community in late] == [
        ("Acme", "Bob", "San Francisco"),
        ("Alice", "Cascade Analytics", "Dana", "Seattle"),
    ]


def test_hybrid_search_fuses_its_retrievers() -> None:
    """The search combines the results of the keyword, similarity, and graph retrievers into one order."""

    graph = make_graph()
    populate(graph)
    search = GraphitiSearch(graph)
    alice = graph.get_entity("Alice")

    fused = search.search("where does Alice work", filters=SearchFilters(as_of=at("2025-08-01")))
    centered = search.search(
        "Seattle",
        center=alice,
        filters=SearchFilters(as_of=at("2025-08-01")),
    )
    limited = search.search("Alice", limit=2)

    assert [hit.text for hit in fused][:2] == [
        "Alice lives in Seattle.",
        "Alice works at Cascade Analytics.",
    ]
    assert [hit.score for hit in fused] == sorted((hit.score for hit in fused), reverse=True)
    assert fused[0].reason == "keyword+similarity+graph then rrf"
    assert RECIPE_NAME == "EDGE_HYBRID_SEARCH_RRF"
    assert len(limited) == 2
    assert {hit.text for hit in centered} > {hit.text for hit in search.search("Seattle")}

    with pytest.raises(ValueError, match="cannot be empty"):
        search.search("  ")
    with pytest.raises(ValueError, match="non-negative integer"):
        search.search("Alice", limit=-1)


def test_search_ranking_never_depends_on_block_ids() -> None:
    """The search gives the same order and scores for 2 identical graphs, although their block ids are different.

    - **Ties use fact content.** The search breaks a tie between equal ranks on
      the content of the fact.
    - **Block ids change on each run.** `MemoryBlock.id` is a new value on each
      run. If a tie break uses it, the rank of a fact inside a retriever
      changes. Its reciprocal-rank contribution then changes too, so the scores
      and the order are different from run to run.
    """

    runs = []
    for _ in range(3):
        graph = make_graph()
        populate(graph)
        runs.append(
            [
                (hit.text, hit.score)
                for hit in GraphitiSearch(graph).search(
                    "where does Alice live and work",
                    center=graph.get_entity("Alice"),
                    filters=SearchFilters(as_of=at("2025-08-01")),
                )
            ]
        )

    assert runs[0] == runs[1] == runs[2]
    assert runs[0], "the query should match at least one fact"


def test_search_filters_restrict_the_candidate_set() -> None:
    """Bi-temporal and relation filters run before retrieval."""

    graph = make_graph()
    populate(graph)
    search = GraphitiSearch(graph)

    now = search.search(
        "Alice lives",
        filters=SearchFilters(as_of=at("2025-08-01"), relations=("lives_in",)),
    )
    then = search.search(
        "Alice lives",
        filters=SearchFilters(known_at=at("2025-06-10"), relations=("lives_in",)),
    )

    assert [hit.text for hit in now] == ["Alice lives in Seattle."]
    assert [hit.text for hit in then] == ["Alice lives in San Francisco."]


def test_graph_partitions_stay_isolated() -> None:
    """Each graph root acts like a Graphiti group id, so graphs under different roots do not share entities or facts."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="test")
    graphs = []
    for name in ("tenant-a", "tenant-b"):
        root = memory.put(type=GRAPH_TYPE, value={"name": name})
        handle = memory.get_handle(root=root)
        graphs.append(
            TemporalKnowledgeGraph(
                handle,
                structures=GraphitiStructures.create(handle, backend),
                ontology=ONTOLOGY,
            )
        )
    first, second = graphs

    episode = first.add_episode(
        name="chat/1",
        content="Alice lives in Seattle.",
        reference_time=at("2025-06-01"),
        ingested_at=at("2025-06-01"),
    )
    first.add_triplet(
        subject="Alice",
        subject_type="person",
        relation="lives_in",
        object="Seattle",
        object_type="city",
        episode=episode,
        valid_at=at("2025-06-01"),
    )

    assert [entity.name for entity in first.entities()] == ["Seattle", "Alice"]
    assert second.entities() == []
    assert second.facts() == []
    assert second.get_entity("Alice") is None


def test_structures_reopen_instead_of_duplicating() -> None:
    """A second `GraphitiStructures.create` call on the same handle reuses the tier anchors that the first call made."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="test")
    root = memory.put(type=GRAPH_TYPE, value={"name": "test"})
    handle = memory.get_handle(root=root)

    first = GraphitiStructures.create(handle, backend)
    second = GraphitiStructures.create(handle, backend)

    assert first.episodic_root.id == second.episodic_root.id
    assert first.semantic_root.id == second.semantic_root.id
    assert first.community_root.id == second.community_root.id
    assert first.valid_time.collection_id != first.transaction_time.collection_id
    assert len(memory.children(root)) == 3


def test_agent_learns_and_answers_only_through_tools() -> None:
    """The example agent writes and reads the graph through named tools."""

    agent = build_agent()
    assert [tool.name for tool in agent.tools] == [
        "add_episode",
        "add_triplet",
        "update_entity_summary",
        "build_communities",
        "snapshot",
        "get_entity_history",
        "search",
        "get_community",
    ]

    agent.learn(OBSERVATIONS, communities_as_of=AS_OF)
    answer = agent.answer(
        "Where does Alice live and work now?",
        subject="Alice",
        as_of=AS_OF,
        known_at=KNOWN_AT,
        history_relation="lives_in",
        query="where does Alice live and work",
    )

    assert "True for Alice as of 2025-08-01:" in answer
    assert "- Alice -[lives_in]-> Seattle" in answer
    assert "On record for Alice on 2025-06-10:" in answer
    assert "- Alice -[lives_in]-> San Francisco" in answer
    assert "true [2024-01-10, 2025-06-01); on record [2024-01-10, 2025-06-20)" in answer
    assert "Community context:\n- Cascade Analytics community:" in answer
    assert agent.tool_usage() == ("add_episode x4, add_triplet x9, update_entity_summary x3, " "build_communities x1, snapshot x2, get_entity_history x1, search x1, " "get_community x1")


def test_agent_rejects_unknown_tools() -> None:
    """The tool boundary rejects an unknown tool name or an unknown argument before it uses memory."""

    tools = TemporalGraphTools(make_graph())
    agent = TemporalGraphAgent(name="test-agent", tools=tools)

    with pytest.raises(ValueError, match="Unknown temporal graph tool"):
        tools.call("delete_everything", {})
    with pytest.raises(TypeError):
        tools.call("search", {"query": "Alice", "recipe": "MAGIC_SEARCH"})
    assert agent.call_log == []


def test_demo_runs_as_a_module() -> None:
    """The demo runs as a module from the repository root."""

    result = subprocess.run(
        [sys.executable, "-m", "examples.lamps.graphiti.demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("== Graphiti-inspired bi-temporal knowledge graph ==\n")
    assert "Genie structures: ownership tree, property graph, event log, " "two temporal collections, versioned records, lineage DAG" in result.stdout
    assert "- add_episode: Store one source record" in result.stdout
    assert "True for Alice as of 2025-08-01:" in result.stdout
    assert "On record for Alice on 2025-06-10:" in result.stdout
    assert "Alice -[lives_in]-> Seattle" in result.stdout
    assert "Alice -[lives_in]-> San Francisco" in result.stdout
    assert "Top facts for 'where does Alice live and work':" in result.stdout
    assert "Community context:\n- Cascade Analytics community:" in result.stdout
    assert "Inspectable tool calls:\n01. add_episode\n02. add_triplet" in result.stdout
    assert "Tool usage:\n- add_episode x4\n- add_triplet x9" in result.stdout
    assert "- search x1" in result.stdout
