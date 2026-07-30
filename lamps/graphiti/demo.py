"""Run the Graphiti-inspired temporal knowledge graph demo.

From the repository root:

    conda run -n "genie" python -m lamps.graphiti.demo
"""

from __future__ import annotations

from datetime import UTC, datetime

from genie import InMemoryBackend, Memory
from lamps.graphiti import TemporalKnowledgeGraph


def at(value: str) -> datetime:
    """Parse a demo date at midnight UTC."""

    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def main() -> None:
    """Build a small graph and run temporal and neighborhood queries."""

    memory = Memory(backend=InMemoryBackend(), principal="graphiti-demo")
    root = memory.put(
        type="temporal_knowledge_graph",
        value={"name": "Customer context"},
    )
    graph = TemporalKnowledgeGraph(memory.get_handle(root=root))

    alice = graph.add_entity("Alice", entity_type="person")
    bob = graph.add_entity("Bob", entity_type="person")
    acme = graph.add_entity("Acme", entity_type="company")
    san_francisco = graph.add_entity("San Francisco", entity_type="city")
    seattle = graph.add_entity("Seattle", entity_type="city")

    first_chat = graph.add_episode(
        "Alice said she lives in San Francisco and works at Acme.",
        source="chat/2024-01-10",
        observed_at=at("2024-01-10"),
    )
    graph.assert_fact(
        alice,
        "lives_in",
        san_francisco,
        episode=first_chat,
        valid_from=at("2024-01-10"),
        exclusive=True,
    )
    alice_employment = graph.assert_fact(
        alice,
        "works_at",
        acme,
        episode=first_chat,
        valid_from=at("2024-01-10"),
    )

    directory = graph.add_episode(
        "Acme is headquartered in San Francisco; Alice and Bob work there.",
        source="directory/2024-02-01",
        observed_at=at("2024-02-01"),
    )
    graph.assert_fact(
        alice,
        "works_at",
        acme,
        episode=directory,
        valid_from=at("2024-02-01"),
    )
    graph.assert_fact(
        bob,
        "works_at",
        acme,
        episode=directory,
        valid_from=at("2024-02-01"),
    )
    graph.assert_fact(
        acme,
        "headquartered_in",
        san_francisco,
        episode=directory,
        valid_from=at("2024-02-01"),
        exclusive=True,
    )

    moving_chat = graph.add_episode(
        "Alice said she moved to Seattle on June 1.",
        source="chat/2025-06-02",
        observed_at=at("2025-06-02"),
    )
    graph.assert_fact(
        alice,
        "lives_in",
        seattle,
        episode=moving_chat,
        valid_from=at("2025-06-01"),
        exclusive=True,
    )

    for query_date in ("2024-12-01", "2025-07-01"):
        print(f"Alice as of {query_date}:")
        for fact in graph.facts_at(at(query_date), subject=alice):
            print(f"- {fact.statement}")

    print("Acme neighborhood as of 2025-07-01:")
    for fact in graph.neighborhood_at(acme, at("2025-07-01")):
        print(f"- {fact.statement}")

    print("Alice location history:")
    for fact in graph.history(alice, relation="lives_in"):
        valid_to = fact.valid_to.date().isoformat() if fact.valid_to else "open"
        sources = ", ".join(
            episode.value["source"] for episode in graph.provenance(fact)
        )
        print(
            f"- {fact.statement}; "
            f"valid [{fact.valid_from.date().isoformat()}, {valid_to}); "
            f"provenance: {sources}"
        )

    employment_sources = ", ".join(
        episode.value["source"] for episode in graph.provenance(alice_employment)
    )
    print(f"Alice employment provenance: {employment_sources}")


if __name__ == "__main__":
    main()
