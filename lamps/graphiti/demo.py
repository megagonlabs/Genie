"""Run an agent with bi-temporal knowledge graph tools.

From the repository root:

    conda run -n "genie" python -m lamps.graphiti.demo
"""

from __future__ import annotations

from genie import InMemoryBackend, Memory
from lamps.demo_output import print_agent_demo
from lamps.graphiti import (
    GRAPH_TYPE,
    CommunityBuilder,
    EdgeType,
    EntityType,
    GraphitiSearch,
    GraphitiStructures,
    Observation,
    Ontology,
    SummaryUpdate,
    TemporalGraphAgent,
    TemporalGraphTools,
    TemporalKnowledgeGraph,
    TripletCandidate,
)

CUSTOMER_ONTOLOGY = Ontology.prescribe(
    entities=[
        EntityType(name="person", description="A customer, colleague, or contact."),
        EntityType(name="company", description="An employer or vendor."),
        EntityType(name="city", description="A place a person or company sits in."),
    ],
    edges=[
        EdgeType(
            name="lives_in",
            description="Where a person currently lives.",
            subject_types=frozenset({"person"}),
            object_types=frozenset({"city"}),
            single_valued=True,
        ),
        EdgeType(
            name="works_at",
            description="Where a person currently works.",
            subject_types=frozenset({"person"}),
            object_types=frozenset({"company"}),
            single_valued=True,
        ),
        EdgeType(
            name="headquartered_in",
            description="Where a company is headquartered.",
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

OBSERVATIONS = (
    Observation(
        name="chat/2024-01-10",
        content="Alice said she lives in San Francisco and works at Acme.",
        reference_time="2024-01-10T00:00:00Z",
        ingested_at="2024-01-10T00:00:00Z",
        source="message",
        source_description="support chat transcript",
        triplets=(
            TripletCandidate(
                subject="Alice",
                subject_type="person",
                relation="lives_in",
                object="San Francisco",
                object_type="city",
                valid_from="2024-01-10T00:00:00Z",
            ),
            TripletCandidate(
                subject="Alice",
                subject_type="person",
                relation="works_at",
                object="Acme",
                object_type="company",
                valid_from="2024-01-10T00:00:00Z",
            ),
        ),
        summaries=(
            SummaryUpdate(
                entity="Alice",
                entity_type="person",
                summary="Alice is a customer in San Francisco who works at Acme.",
            ),
        ),
    ),
    Observation(
        name="directory/2024-02-01",
        content=(
            "Acme is headquartered in San Francisco. Bob works there and manages "
            "Alice until she leaves in July 2025."
        ),
        reference_time="2024-02-01T00:00:00Z",
        ingested_at="2024-02-01T00:00:00Z",
        source="text",
        source_description="company directory export",
        triplets=(
            TripletCandidate(
                subject="Acme",
                subject_type="company",
                relation="headquartered_in",
                object="San Francisco",
                object_type="city",
                valid_from="2024-02-01T00:00:00Z",
            ),
            TripletCandidate(
                subject="Bob",
                subject_type="person",
                relation="works_at",
                object="Acme",
                object_type="company",
                valid_from="2024-02-01T00:00:00Z",
            ),
            TripletCandidate(
                subject="Bob",
                subject_type="person",
                relation="manages",
                object="Alice",
                object_type="person",
                valid_from="2024-02-01T00:00:00Z",
                valid_to="2025-07-01T00:00:00Z",
            ),
        ),
    ),
    Observation(
        name="chat/2025-06-20",
        content="Alice mentioned in passing that she moved to Seattle back on June 1.",
        reference_time="2025-06-02T00:00:00Z",
        ingested_at="2025-06-20T00:00:00Z",
        source="message",
        source_description="support chat transcript",
        triplets=(
            TripletCandidate(
                subject="Alice",
                subject_type="person",
                relation="lives_in",
                object="Seattle",
                object_type="city",
                valid_from="2025-06-01T00:00:00Z",
            ),
        ),
        summaries=(
            SummaryUpdate(
                entity="Alice",
                entity_type="person",
                summary="Alice moved to Seattle in June 2025 and still works at Acme.",
            ),
        ),
    ),
    Observation(
        name="news/2025-07-05",
        content="Cascade Analytics, headquartered in Seattle, hired Alice. Dana already worked there.",
        reference_time="2025-07-05T00:00:00Z",
        ingested_at="2025-07-05T00:00:00Z",
        source="text",
        source_description="industry newsletter",
        triplets=(
            TripletCandidate(
                subject="Cascade Analytics",
                subject_type="company",
                relation="headquartered_in",
                object="Seattle",
                object_type="city",
                valid_from="2025-06-25T00:00:00Z",
            ),
            TripletCandidate(
                subject="Dana",
                subject_type="person",
                relation="works_at",
                object="Cascade Analytics",
                object_type="company",
                valid_from="2025-06-25T00:00:00Z",
            ),
            TripletCandidate(
                subject="Alice",
                subject_type="person",
                relation="works_at",
                object="Cascade Analytics",
                object_type="company",
                valid_from="2025-07-01T00:00:00Z",
            ),
        ),
        summaries=(
            SummaryUpdate(
                entity="Alice",
                entity_type="person",
                summary="Alice lives in Seattle and works at Cascade Analytics.",
            ),
        ),
    ),
)

AS_OF = "2025-08-01T00:00:00Z"
KNOWN_AT = "2025-06-10T00:00:00Z"


def build_agent() -> TemporalGraphAgent:
    """Assemble an empty graph, its tool set, and an agent over them.

    Returns:
        An agent whose only graph access is the named tool set.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="graphiti-demo")
    root = memory.put(type=GRAPH_TYPE, value={"name": "Customer context"})
    handle = memory.get_handle(root=root)
    graph = TemporalKnowledgeGraph(
        handle,
        structures=GraphitiStructures.create(handle, backend),
        ontology=CUSTOMER_ONTOLOGY,
    )
    return TemporalGraphAgent(
        name="context-agent",
        tools=TemporalGraphTools(
            graph,
            communities=CommunityBuilder(graph),
            search=GraphitiSearch(graph),
        ),
    )


def main() -> None:
    """Let an agent ingest episodes and answer through graph tools."""

    agent = build_agent()
    agent.learn(OBSERVATIONS, communities_as_of=AS_OF)
    answer = agent.answer(
        "Where does Alice live and work now, and what did we believe in June?",
        subject="Alice",
        as_of=AS_OF,
        known_at=KNOWN_AT,
        history_relation="lives_in",
        query="where does Alice live and work",
    )
    print_agent_demo(
        title="Graphiti-inspired bi-temporal knowledge graph",
        purpose=(
            "An agent ingests episodes, invalidates contradicted facts on two "
            "time axes, groups entities by slice, and ranks the result."
        ),
        structures=(
            "ownership tree",
            "property graph",
            "event log",
            "two temporal collections",
            "versioned records",
            "lineage DAG",
        ),
        tools=agent.tools,
        answer=answer,
        calls=agent.call_log,
    )


if __name__ == "__main__":
    main()
