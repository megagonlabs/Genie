"""Tests for the Hindsight-inspired agent-memory lamp."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from examples.lamps.hindsight import (
    BANK_TYPE,
    CAUSAL_RELATION,
    DERIVED_FROM_RELATION,
    MEMORY_TYPE,
    MENTIONS_RELATION,
    TEMPORAL_RELATION,
    Claim,
    ConsolidationEngine,
    FactDraft,
    HindsightMemoryBank,
    HindsightRecall,
    HindsightStructures,
    MentalModelStore,
    RecallWindow,
    Reflector,
)
from examples.lamps.hindsight.demo import DOCUMENTS, ENTITIES, MODELS, build_agent
from examples.lamps.hindsight.model import ENTITY_NAMESPACE, OBSERVATION_REVISION_TYPE
from examples.lamps.hindsight.structures import TIERS
from genie import AccessDenied, InMemoryBackend, Memory
from genie.structures import MemoryAddress, ValidityInterval

REPO_ROOT = Path(__file__).resolve().parents[4]


def at(value: str) -> datetime:
    """Parse a test timestamp into UTC, and read a date without a time as midnight UTC."""

    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class Bank:
    """One assembled bank with every layer the lamp builds on it."""

    def __init__(self, name: str = "test") -> None:
        """Create an empty bank rooted in a bounded Genie handle."""

        self.backend = InMemoryBackend()
        self.memory = Memory(backend=self.backend, principal="test")
        self.root = self.memory.put(type=BANK_TYPE, value={"name": name})
        self.handle = self.memory.get_handle(root=self.root)
        self.bank = HindsightMemoryBank(
            self.handle,
            structures=HindsightStructures.create(self.handle, self.backend),
        )
        self.consolidation = ConsolidationEngine(self.bank)
        self.models = MentalModelStore(self.bank, self.consolidation)
        self.recall = HindsightRecall(self.bank, self.consolidation)
        self.reflector = Reflector(self.bank, self.models, self.recall)


def draft(
    text: str,
    *,
    fact_type: str = "world",
    entities: tuple[str, ...] = (),
    occurred_at: str | None = None,
    occurred_until: str | None = None,
    tags: tuple[str, ...] = (),
    claim: str | None = None,
    belief: str | None = None,
    stance: str = "supports",
) -> FactDraft:
    """Build one draft of an extracted fact, with default values that suit the tests."""

    return FactDraft(
        text=text,
        fact_type=cast(Any, fact_type),
        entities=entities,
        occurred_at=None if occurred_at is None else at(occurred_at),
        occurred_until=None if occurred_until is None else at(occurred_until),
        tags=tags,
        claim=(
            None
            if claim is None
            else Claim(key=claim, belief=cast(str, belief), stance=cast(Any, stance))
        ),
    )


def test_bank_tiers_are_owned_and_reopen_instead_of_duplicating() -> None:
    """One root owns every tier, and a second `HindsightStructures.create` call reuses the same tiers."""

    fixture = Bank()
    tiers = {child.metadata["tier"]: child for child in fixture.handle.children()}
    reopened = HindsightStructures.create(fixture.handle, fixture.backend)

    assert set(tiers) == set(TIERS)
    assert reopened.memories.id == fixture.bank.structures.memories.id
    assert reopened.documents.id == fixture.bank.structures.documents.id
    assert len(fixture.handle.children()) == len(TIERS)

    fixture.memory.delete(fixture.root)
    assert fixture.memory.get(tiers["memories"].id) is None


def test_retain_stores_the_document_and_both_time_axes_of_each_fact() -> None:
    """One temporal entry records when the bank learned a fact and, if known, the time that the fact describes."""

    fixture = Bank()
    result = fixture.bank.retain(
        source="chat/1",
        content="Alice moved the migration to small batches on March 9.",
        retained_at=at("2025-03-10T08:00:00Z"),
        context="planning call",
        facts=(
            draft(
                "I retried the migration in small batches.",
                fact_type="experience",
                entities=("Alice",),
                occurred_at="2025-03-09T10:00:00Z",
                occurred_until="2025-03-09T10:30:00Z",
                tags=("billing",),
            ),
            draft("Alice prefers small batches.", entities=("Alice",), tags=("billing",)),
        ),
    )

    experience, preference = result.facts
    timed = fixture.bank.structures.timeline.get(experience.block)
    ongoing = fixture.bank.structures.timeline.get(preference.block)

    assert result.document.source == "chat/1"
    assert result.duplicates == ()
    assert experience.block.parent_id == fixture.bank.structures.memories.id
    assert timed is not None and ongoing is not None
    assert timed.entry.occurred_at == at("2025-03-10T08:00:00Z")
    assert timed.entry.validity == ValidityInterval(
        valid_at=at("2025-03-09T10:00:00Z"),
        invalid_at=at("2025-03-09T10:30:00Z"),
    )
    assert ongoing.entry.occurred_at == at("2025-03-10T08:00:00Z")
    assert ongoing.entry.validity is None
    assert [fact.id for fact in fixture.bank.facts()] == [experience.id, preference.id]
    assert fixture.bank.provenance(experience.block) == (result.document.block,)


def test_a_repeated_fact_gains_provenance_instead_of_a_duplicate() -> None:
    """If a new document repeats a stored fact, the bank adds the document as provenance and does not store a duplicate."""

    fixture = Bank()
    first = fixture.bank.retain(
        source="chat/1",
        content="Billing runs on Postgres.",
        retained_at=at("2025-01-01"),
        facts=(draft("Billing runs on Postgres."),),
    )
    second = fixture.bank.retain(
        source="chat/2",
        content="Confirming billing runs on Postgres.",
        retained_at=at("2025-02-01"),
        facts=(draft("  Billing runs on Postgres.  "),),
    )

    assert second.facts == first.facts
    assert second.duplicates == first.facts
    assert len(fixture.bank.facts()) == 1
    assert {block.id for block in fixture.bank.provenance(first.facts[0].block)} == {
        first.document.id,
        second.document.id,
    }


def test_entity_resolution_binds_every_surface_form_to_one_entity() -> None:
    """The bank stores a resolved alias as a keyed address, so the next lookup of that alias is a read."""

    fixture = Bank()
    fixture.bank.register_entity("Alice Chen", kind="person", aliases=("Alice",))
    fixture.bank.retain(
        source="chat/1",
        content="Alice C. and Alice Cooper both spoke.",
        retained_at=at("2025-01-01"),
        facts=(draft("Alice C. leads the platform team.", entities=("Alice C.",)),),
    )

    chen = fixture.bank.entity("Alice Chen")
    assert chen is not None
    assert fixture.bank.entity("Alice") == chen
    assert fixture.bank.entity("alice c") == chen
    assert chen.aliases == ("Alice Chen", "Alice", "Alice C.")
    assert (
        fixture.bank.structures.entities.get(MemoryAddress(ENTITY_NAMESPACE, "alice c")) is not None
    )
    assert [entity.name for entity in fixture.bank.entities()] == ["Alice Chen"]

    unrelated = fixture.bank.resolve_entity("Bob")
    assert unrelated.name == "Bob"
    assert len(fixture.bank.entities()) == 2


def test_an_ambiguous_name_starts_a_new_entity_rather_than_guessing() -> None:
    """An abbreviation resolves only if exactly one canonical name matches, and otherwise the bank creates a new entity."""

    fixture = Bank()
    fixture.bank.register_entity("Alice Chen", kind="person")
    fixture.bank.register_entity("Alice Cooper", kind="person")

    resolved = fixture.bank.resolve_entity("Alice", kind="person")

    assert resolved.name == "Alice"
    assert len(fixture.bank.entities()) == 3


def test_mentions_are_graph_edges_from_facts_to_entities() -> None:
    """The bank stores each connection from a fact to an entity as a link, not as a repeated name in a payload."""

    fixture = Bank()
    fixture.bank.retain(
        source="chat/1",
        content="Billing and frontend notes.",
        retained_at=at("2025-01-01"),
        facts=(
            draft("Billing runs on Postgres.", entities=("Billing",)),
            draft("The frontend is Vue.", entities=("Frontend",)),
        ),
    )

    billing = fixture.bank.entity("Billing")
    assert billing is not None
    edges = fixture.bank.structures.graph.edges(billing.block, direction="in")

    assert [edge.relation for edge in edges] == [MENTIONS_RELATION]
    assert [fact.text for fact in fixture.bank.mentioned_by("Billing")] == [
        "Billing runs on Postgres."
    ]


def test_invalidation_retires_a_fact_without_deleting_it() -> None:
    """After `invalidate`, recall and `facts` do not return the fact, but the fact stays readable and keeps its provenance."""

    fixture = Bank()
    result = fixture.bank.retain(
        source="chat/1",
        content="Billing runs on MySQL.",
        retained_at=at("2025-01-01"),
        facts=(draft("Billing runs on MySQL.", entities=("Billing",)),),
    )
    stored = result.facts[0]

    retired = fixture.bank.invalidate(stored)

    assert retired.state == "invalidated"
    assert fixture.bank.facts() == []
    assert [fact.id for fact in fixture.bank.facts(include_invalidated=True)] == [stored.id]
    assert fixture.bank.fact(stored.id) is not None
    assert fixture.bank.provenance(stored.block) == (result.document.block,)
    assert fixture.recall.recall("MySQL").hits == ()


def test_consolidation_strengthens_extends_and_reconciles_one_belief() -> None:
    """New evidence raises the proof count, and a reversal writes a new version that keeps the old text."""

    fixture = Bank()
    fixture.bank.retain(
        source="chat/1",
        content="React today.",
        retained_at=at("2025-01-01"),
        facts=(
            draft(
                "Alice says the frontend is React.",
                tags=("web",),
                claim="stack",
                belief="The frontend is React.",
            ),
        ),
    )
    first = fixture.consolidation.consolidate(at=at("2025-01-01"))

    fixture.bank.retain(
        source="chat/2",
        content="Still React.",
        retained_at=at("2025-02-01"),
        facts=(
            draft(
                "Bob also says the frontend is React.",
                tags=("web",),
                claim="stack",
                belief="The frontend is React.",
            ),
        ),
    )
    strengthened = fixture.consolidation.consolidate(at=at("2025-02-01"))

    fixture.bank.retain(
        source="chat/3",
        content="Now Vue.",
        retained_at=at("2025-03-01"),
        facts=(
            draft(
                "Alice moved the frontend to Vue.",
                tags=("web",),
                claim="stack",
                belief="The frontend is Vue.",
                stance="contradicts",
            ),
        ),
    )
    reconciled = fixture.consolidation.consolidate(at=at("2025-03-01"))

    assert (first[0].revision, first[0].proof_count) == (1, 1)
    assert (strengthened[0].revision, strengthened[0].proof_count) == (1, 2)
    assert strengthened[0].text == "The frontend is React."
    assert (reconciled[0].revision, reconciled[0].proof_count) == (2, 3)
    assert reconciled[0].text == "The frontend is Vue. Previously: The frontend is React."
    assert reconciled[0].stance == "contradicts"
    assert [item.text for item in fixture.consolidation.history("stack")] == [
        "The frontend is React.",
        "The frontend is Vue. Previously: The frontend is React.",
    ]
    assert reconciled[0].block.parent_id == reconciled[0].identity.id


def test_a_proof_count_is_counted_off_the_lineage_graph() -> None:
    """The bank stores evidence as lineage edges, so the proof count always agrees with the facts."""

    fixture = Bank()
    result = fixture.bank.retain(
        source="chat/1",
        content="Two supporting facts.",
        retained_at=at("2025-01-01"),
        facts=(
            draft("Batches of ten finish.", claim="batching", belief="Small batches finish."),
            draft("Batches of twenty finish.", claim="batching", belief="Small batches finish."),
        ),
    )
    observation = fixture.consolidation.consolidate(at=at("2025-01-02"))[0]

    edges = fixture.bank.structures.lineage.edges(observation.block, direction="in")

    assert observation.proof_count == 2
    assert set(observation.sources) == {fact.id for fact in result.facts}
    assert {edge.relation for edge in edges} == {DERIVED_FROM_RELATION}


def test_an_observation_reports_itself_stale_until_the_next_consolidation() -> None:
    """The retention cursor marks an observation as stale until the next consolidation reads the newer evidence."""

    fixture = Bank()
    fixture.bank.retain(
        source="chat/1",
        content="First.",
        retained_at=at("2025-01-01"),
        facts=(draft("Batches of ten finish.", claim="batching", belief="Small batches finish."),),
    )
    fixture.consolidation.consolidate(at=at("2025-01-01"))
    assert fixture.consolidation.cursor() == 1
    consolidated = fixture.consolidation.observation("batching")
    assert consolidated is not None
    assert consolidated.stale is False

    fixture.bank.retain(
        source="chat/2",
        content="Second.",
        retained_at=at("2025-02-01"),
        facts=(draft("Batches of five finish.", claim="batching", belief="Small batches finish."),),
    )

    assert [fact.text for fact in fixture.consolidation.pending()] == ["Batches of five finish."]
    outdated = fixture.consolidation.observation("batching")
    assert outdated is not None
    assert outdated.stale is True


def test_invalidated_facts_are_skipped_by_consolidation() -> None:
    """Consolidation skips an invalidated fact, so that fact no longer supports the belief of its claim."""

    fixture = Bank()
    result = fixture.bank.retain(
        source="chat/1",
        content="Both claims.",
        retained_at=at("2025-01-01"),
        facts=(
            draft("Billing is MySQL.", claim="store", belief="Billing runs on MySQL."),
            draft("Batches of ten finish.", claim="batching", belief="Small batches finish."),
        ),
    )
    fixture.bank.invalidate(result.facts[0])

    observations = fixture.consolidation.consolidate(at=at("2025-01-02"))

    assert [item.claim for item in observations] == ["batching"]
    assert fixture.consolidation.observation("store") is None


def test_a_mental_model_is_rebuilt_only_when_its_answer_changes() -> None:
    """`refresh` writes a new mental model version only if the answer changes, not for more evidence on a known belief."""

    fixture = Bank()
    fixture.models.create(
        name="brief",
        question="What should I know?",
        tags=("web",),
    )
    fixture.bank.retain(
        source="chat/1",
        content="React.",
        retained_at=at("2025-01-01"),
        facts=(
            draft("Alice says React.", tags=("web",), claim="stack", belief="Frontend is React."),
        ),
    )
    fixture.consolidation.consolidate(at=at("2025-01-01"))
    first = fixture.models.refresh("brief", at=at("2025-01-01"))

    fixture.bank.retain(
        source="chat/2",
        content="React again.",
        retained_at=at("2025-02-01"),
        facts=(draft("Bob says React.", tags=("web",), claim="stack", belief="Frontend is React."),),
    )
    fixture.consolidation.consolidate(at=at("2025-02-01"))
    unchanged = fixture.models.refresh("brief", at=at("2025-02-01"))

    fixture.bank.retain(
        source="chat/3",
        content="Vue.",
        retained_at=at("2025-03-01"),
        facts=(
            draft(
                "Alice says Vue.",
                tags=("web",),
                claim="stack",
                belief="Frontend is Vue.",
                stance="contradicts",
            ),
        ),
    )
    fixture.consolidation.consolidate(at=at("2025-03-01"))
    rebuilt = fixture.models.refresh("brief", at=at("2025-03-01"))

    assert first.revision == 1
    assert first.content == "Frontend is React."
    assert unchanged.revision == 1
    assert rebuilt.revision == 2
    assert rebuilt.content == "Frontend is Vue. Previously: Frontend is React."
    assert [version.revision for version in fixture.models.history("brief")] == [1, 2]
    belief = fixture.consolidation.observation("stack")
    assert belief is not None
    assert rebuilt.sources == (belief.id,)


def test_a_mental_model_reads_only_the_memories_in_its_scope() -> None:
    """The scope tags of a mental model decide which beliefs its answer includes."""

    fixture = Bank()
    fixture.models.create(name="web", question="Frontend?", tags=("web",))
    fixture.models.create(name="all", question="Everything?")
    fixture.bank.retain(
        source="chat/1",
        content="Two areas.",
        retained_at=at("2025-01-01"),
        facts=(
            draft("Frontend is Vue.", tags=("web",), claim="stack", belief="Frontend is Vue."),
            draft("Billing is Postgres.", tags=("data",), claim="store", belief="Billing is Postgres."),
        ),
    )
    fixture.consolidation.consolidate(at=at("2025-01-02"))

    scoped = fixture.models.refresh("web", at=at("2025-01-02"))
    everything = fixture.models.refresh("all", at=at("2025-01-02"))

    assert scoped.content == "Frontend is Vue."
    assert everything.content == "Frontend is Vue. Billing is Postgres."
    with pytest.raises(ValueError, match="already exists"):
        fixture.models.create(name="web", question="Frontend?")


def seed_recall_bank() -> Bank:
    """Build a bank with facts spread across entities, links, and time."""

    fixture = Bank()
    fixture.bank.register_entity("Northwind billing", kind="service", aliases=("billing",))
    first = fixture.bank.retain(
        source="run/1",
        content="Timeout.",
        retained_at=at("2025-02-03"),
        facts=(
            draft(
                "The single-batch migration timed out.",
                fact_type="experience",
                entities=("Northwind billing",),
                occurred_at="2025-02-03T17:00:00Z",
                tags=("billing",),
                claim="batching",
                belief="Migrations finish only in small batches.",
            ),
        ),
    )
    second = fixture.bank.retain(
        source="run/2",
        content="Retry.",
        retained_at=at("2025-03-09"),
        facts=(
            draft(
                "The small-batch retry succeeded.",
                fact_type="experience",
                occurred_at="2025-03-09T10:00:00Z",
                tags=("billing",),
            ),
            draft("Unrelated marketing copy was approved.", tags=("marketing",)),
        ),
    )
    fixture.bank.connect(first.facts[0], second.facts[0], TEMPORAL_RELATION)
    fixture.bank.connect(first.facts[0], second.facts[0], CAUSAL_RELATION)
    fixture.consolidation.consolidate(at=at("2025-03-10"))
    return fixture


def test_recall_reports_which_retrievers_reached_each_result() -> None:
    """Recall runs 4 retrievers for each query, combines them with reciprocal rank fusion, and reports which retrievers reached each hit."""

    fixture = seed_recall_bank()

    response = fixture.recall.recall(
        "migration batches",
        entity="billing",
        window=RecallWindow(start=at("2025-02-01"), end=at("2025-02-28")),
        max_tokens=200,
    )
    reached = {hit.text: set(hit.retrievers) for hit in response.hits}

    assert reached["The single-batch migration timed out."] == {
        "similarity",
        "keyword",
        "graph",
        "temporal",
    }
    assert "temporal" not in reached["The small-batch retry succeeded."]
    assert "graph" in reached["Migrations finish only in small batches."]
    assert response.hits[0].score >= response.hits[-1].score


def test_recall_stops_at_its_token_budget() -> None:
    """Like Hindsight, recall fills a token budget and does not return a fixed number of rows."""

    fixture = seed_recall_bank()

    generous = fixture.recall.recall("migration batches", max_tokens=200)
    tight = fixture.recall.recall("migration batches", max_tokens=6)

    assert generous.dropped == 0
    assert tight.tokens_used <= 6
    assert tight.dropped == len(generous.hits) - len(tight.hits)
    assert [hit.text for hit in tight.hits] == [hit.text for hit in generous.hits][: len(tight.hits)]


def test_recall_filters_by_type_and_tag() -> None:
    """Recall returns observations together with raw facts if the caller does not exclude them."""

    fixture = seed_recall_bank()

    facts_only = fixture.recall.recall("migration batches", types=("experience",), max_tokens=200)
    tagged = fixture.recall.recall("migration approved", tags=("marketing",), max_tokens=200)

    assert {hit.kind for hit in facts_only.hits} == {"experience"}
    assert [hit.text for hit in tagged.hits] == ["Unrelated marketing copy was approved."]
    with pytest.raises(ValueError, match="Unsupported recall types"):
        fixture.recall.recall("migration", types=("opinion",))


def test_the_temporal_retriever_reads_both_axes() -> None:
    """A recall window matches facts that the bank learned inside it and facts that describe a time inside it."""

    fixture = Bank()
    fixture.bank.retain(
        source="chat/1",
        content="Backfilled note.",
        retained_at=at("2025-06-01"),
        facts=(draft("The outage happened in March.", occurred_at="2025-03-15"),),
    )
    fixture.bank.retain(
        source="chat/2",
        content="Same-day note.",
        retained_at=at("2025-03-20"),
        facts=(draft("A note written during March.", fact_type="experience"),),
    )

    window = RecallWindow(start=at("2025-03-01"), end=at("2025-04-01"))
    learned = fixture.bank.learned_between(window.start, window.end)
    described = fixture.bank.describing(window.as_interval())
    hits = fixture.recall.recall("March", window=window, max_tokens=200).hits

    assert [fact.text for fact in learned] == ["A note written during March."]
    assert [fact.text for fact in described] == ["The outage happened in March."]
    assert {hit.text for hit in hits} == {
        "The outage happened in March.",
        "A note written during March.",
    }


def test_reflection_answers_from_the_highest_layer_that_knows() -> None:
    """Reflection answers from the mental model first, then from observations, and then from raw facts."""

    fixture = seed_recall_bank()

    from_facts = fixture.reflector.reflect(
        "What happened?",
        query="marketing copy",
        tags=("marketing",),
    )
    fixture.models.create(name="brief", question="What should I know?", tags=("billing",))

    from_observations = fixture.reflector.reflect(
        "How should migrations run?",
        query="migration batches",
        tags=("billing",),
    )
    fixture.models.refresh("brief", at=at("2025-03-10"))
    from_model = fixture.reflector.reflect(
        "How should migrations run?",
        model_name="brief",
        query="migration batches",
    )

    assert from_facts.trace == ("search_mental_models", "search_observations", "recall")
    assert "marketing" in from_facts.text
    assert from_observations.text == "Migrations finish only in small batches."
    assert from_model.text == "Migrations finish only in small batches."
    assert from_model.based_on[0].kind == "mental_model"
    assert from_observations.based_on[0].kind == "observation"


def test_reflection_reports_the_provenance_of_every_piece_of_evidence() -> None:
    """For each layer of evidence, reflection follows the lineage relation and names the sources of that layer."""

    fixture = seed_recall_bank()
    fixture.models.create(name="brief", question="What should I know?", tags=("billing",))
    fixture.models.refresh("brief", at=at("2025-03-10"))

    reflection = fixture.reflector.reflect(
        "How should migrations run?",
        model_name="brief",
        query="migration batches",
        max_tokens=200,
    )
    by_kind = {item.kind: item for item in reflection.based_on}

    observation = fixture.consolidation.observation("batching")
    assert observation is not None
    assert by_kind["mental_model"].sources == (observation.id,)
    assert by_kind["observation"].sources == observation.sources
    assert len(by_kind["experience"].sources) == 1
    source = fixture.bank.memory.get(by_kind["experience"].sources[0])
    assert source is not None
    assert source.type != MEMORY_TYPE


def test_reflection_warns_when_a_belief_has_unconsolidated_evidence() -> None:
    """Reflection adds an `Unverified:` note to the answer when it uses a stale observation."""

    fixture = seed_recall_bank()
    fixture.bank.retain(
        source="run/3",
        content="Another retry.",
        retained_at=at("2025-04-01"),
        facts=(
            draft(
                "A third small-batch run succeeded.",
                fact_type="experience",
                tags=("billing",),
                claim="batching",
                belief="Migrations finish only in small batches.",
            ),
        ),
    )

    reflection = fixture.reflector.reflect(
        "How should migrations run?",
        query="migration batches",
        tags=("billing",),
    )

    assert "Unverified: batching" in reflection.text


def test_one_bank_cannot_read_another() -> None:
    """A bounded handle isolates each memory bank, so one bank cannot read the facts of another bank."""

    first = Bank("first")
    second = Bank("second")
    stored = first.bank.retain(
        source="chat/1",
        content="Private.",
        retained_at=at("2025-01-01"),
        facts=(draft("A private fact."),),
    ).facts[0]

    assert second.bank.facts() == []
    assert second.recall.recall("private").hits == ()
    with pytest.raises(AccessDenied):
        second.handle.get(stored.id)


def test_validation_keeps_the_lamp_model_explicit() -> None:
    """The lamp raises `ValueError` for a bad fact type, a bad time value, an unknown relation, a bad claim stance, or an unknown memory."""

    fixture = Bank()
    with pytest.raises(ValueError, match="fact_type"):
        fixture.bank.retain(
            source="chat/1",
            content="bad",
            retained_at=at("2025-01-01"),
            facts=(draft("bad", fact_type="opinion"),),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        fixture.bank.retain(
            source="chat/1",
            content="bad",
            retained_at=datetime(2025, 1, 1),  # noqa: DTZ001
        )
    with pytest.raises(ValueError, match="occurred_until requires"):
        fixture.bank.retain(
            source="chat/1",
            content="bad",
            retained_at=at("2025-01-01"),
            facts=(draft("bad", occurred_until="2025-01-02"),),
        )
    with pytest.raises(ValueError, match="stance"):
        Claim(key="k", belief="b", stance=cast(Any, "maybe"))

    stored = fixture.bank.retain(
        source="chat/1",
        content="ok",
        retained_at=at("2025-01-01"),
        facts=(draft("first"), draft("second")),
    ).facts
    with pytest.raises(ValueError, match="Unsupported Hindsight relation"):
        fixture.bank.connect(stored[0], stored[1], "hindsight.unknown")
    with pytest.raises(ValueError, match="Unknown memory"):
        fixture.bank.invalidate("missing")


def test_agent_learns_and_answers_only_through_tools() -> None:
    """The agent uses memory only through named tools that take and return JSON-compatible values."""

    agent = build_agent()
    assert [tool.name for tool in agent.tools] == [
        "register_entity",
        "retain",
        "link_memories",
        "invalidate_memory",
        "consolidate",
        "create_mental_model",
        "refresh_mental_model",
        "get_mental_model",
        "recall",
        "reflect",
    ]
    assert not hasattr(agent, "_bank")

    agent.learn(DOCUMENTS, entities=ENTITIES, models=MODELS)
    answer = agent.answer(
        "What should I know before I change Northwind billing?",
        model_name="northwind-change-brief",
        query="Northwind billing migration batches",
        entity="billing service",
        window_start="2025-03-01T00:00:00Z",
        window_end="2025-04-01T00:00:00Z",
    )

    refreshes = [record for record in agent.call_log if record.name == "refresh_mental_model"]
    consolidations = [record for record in agent.call_log if record.name == "consolidate"]

    assert [record.result["rebuilt"] for record in refreshes] == [True, True, False, True]
    assert [record.result["revision"] for record in refreshes] == [1, 2, 2, 3]
    assert consolidations[-1].result["observations"][0]["proof_count"] == 2
    assert "Mental model northwind-change-brief r3" in answer
    assert "was r1: Northwind frontends are built in React." in answer
    assert "similarity+keyword+graph+temporal" in answer
    assert "search_mental_models -> search_observations -> recall" in answer
    assert agent.tool_usage() == (
        "register_entity x2, create_mental_model x1, retain x4, consolidate x4, "
        "refresh_mental_model x4, link_memories x3, invalidate_memory x1, "
        "get_mental_model x1, recall x1, reflect x1"
    )


def test_tool_results_stay_json_compatible() -> None:
    """Tool arguments and results survive a JSON round trip, which is the form that a model uses when it calls functions."""

    agent = build_agent()
    agent.learn(DOCUMENTS[:1], entities=ENTITIES, models=MODELS)

    for record in agent.call_log:
        assert json.loads(json.dumps(record.arguments)) == record.arguments
        assert json.loads(json.dumps(record.result)) == record.result

    with pytest.raises(ValueError, match="Unknown Hindsight tool"):
        agent._toolbox.call("teleport", {})


def test_observation_revisions_are_owned_by_their_belief() -> None:
    """A delete of a belief also deletes its history, because the belief owns its revision blocks."""

    fixture = Bank()
    fixture.bank.retain(
        source="chat/1",
        content="One claim.",
        retained_at=at("2025-01-01"),
        facts=(draft("Small batches finish.", claim="batching", belief="Small batches finish."),),
    )
    observation = fixture.consolidation.consolidate(at=at("2025-01-02"))[0]

    fixture.bank.structures.tiers.delete_subtree(observation.identity)

    assert fixture.memory.get(observation.block.id) is None
    assert fixture.memory.get(observation.identity.id) is None
    assert list(fixture.bank.memory.search(types=[OBSERVATION_REVISION_TYPE], limit=10)) == []


def test_demo_runs_as_a_module() -> None:
    """The demo runs as a module from the repository root."""

    result = subprocess.run(
        [sys.executable, "-m", "examples.lamps.hindsight.demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("== Hindsight-inspired agent memory ==\n")
    assert "Genie structures: ownership tree, event log, temporal collection" in result.stdout
    assert "Tools available to the agent:" in result.stdout
    assert "Mental model northwind-change-brief r3:" in result.stdout
    assert "Reflection (search_mental_models -> search_observations -> recall):" in result.stdout
    assert "Inspectable tool calls:\n01. register_entity\n02. register_entity" in result.stdout
    assert "Tool usage:\n- register_entity x2\n" in result.stdout


def test_demo_output_is_stable_between_runs() -> None:
    """The demo does not read a clock, a network, or a block id, so 2 runs give the same output."""

    outputs = [
        subprocess.run(
            [sys.executable, "-m", "examples.lamps.hindsight.demo"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for _ in range(2)
    ]

    assert outputs[0] == outputs[1]
