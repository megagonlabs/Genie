"""The bi-temporal knowledge graph behavior of the Graphiti lamp.

Graphiti ingests a stream of episodes and extracts entities and relationship
facts from them.

- Every fact sits on a bi-temporal timeline: two timestamps say when it was true
  in the world, and two more say when the system believed it.
- A contradicted fact is invalidated rather than deleted, so the graph can
  answer both "what was true then" and "what did we know then".

This module owns those rules. Genie supplies blocks, ownership, scoped access,
links, search, and the structure facades. The graph class decides what an
episode, entity, fact, provenance edge, mention, and validity interval mean, and
in what order a write resolves entities, dedupes, and invalidates.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from examples.lamps.graphiti.model import (
    ENTITY_NAMESPACE,
    ENTITY_TYPE,
    EPISODE_SOURCES,
    EPISODE_TYPE,
    EPOCH,
    FACT_TYPE,
    MENTIONS_RELATION,
    OBJECT_RELATION,
    PROVENANCE_RELATION,
    SEARCH_LIMIT,
    SUBJECT_RELATION,
    SUMMARY_TYPE,
    Entity,
    Episode,
    EpisodeSource,
    SummaryRevision,
    TemporalFact,
    TripletResult,
    default_sentence,
    entity_address,
    format_timestamp,
    normalize_timestamp,
    parse_timestamp,
    require_text,
    required_metadata,
    required_string,
)
from examples.lamps.graphiti.ontology import Ontology
from examples.lamps.graphiti.structures import GraphitiStructures
from genie import MemoryBlock, MemoryHandle
from genie.structures import MemoryAddress, TemporalCollection, ValidityInterval


class TemporalKnowledgeGraph:
    """Graphiti-inspired bi-temporal graph over a bounded Genie handle.

    The graph owns every episode, entity, summary revision, fact, and community
    below one root. All taxonomy, contradiction, and time semantics live here;
    Genie only stores blocks, links, and structure membership.

    A graph partition corresponds to one Graphiti `group_id`: separate roots
    with separate handles cannot see each other's nodes, so namespacing is a
    property of the handle rather than a field on every record.
    """

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: GraphitiStructures,
        ontology: Ontology,
    ) -> None:
        """Create a graph inside an existing bounded memory handle.

        Args:
            memory: Handle rooted at the block that owns the whole graph.
            structures: Structural views over that same handle.
            ontology: Declared or learned entity and edge types.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("TemporalKnowledgeGraph requires a bounded MemoryHandle.")
        self.memory = memory
        self.structures = structures
        self.ontology = ontology

    # -- Episodic tier ---------------------------------------------------

    def add_episode(
        self,
        *,
        name: str,
        content: str,
        reference_time: datetime,
        ingested_at: datetime,
        source: EpisodeSource = "text",
        source_description: str = "",
    ) -> Episode:
        """Append one source record to the episodic subgraph.

        The episode joins the ingestion event log and receives its reference
        time as event time. Ingestion times must not move backwards, because
        transaction time is the clock that closes superseded facts.

        Args:
            name: Short episode name.
            content: Raw episode body.
            reference_time: When the source event happened.
            ingested_at: When this graph processed the episode.
            source: Episode kind, mirroring Graphiti's `EpisodeType`.
            source_description: Free-text description of the origin.

        Returns:
            The stored episode.

        Raises:
            ValueError: If a field is empty, a timestamp is naive, the source
                kind is unknown, or `ingested_at` precedes the newest episode.
        """

        require_text(name, field="Episode name")
        require_text(content, field="Episode content")
        if source not in EPISODE_SOURCES:
            raise ValueError(f"Unknown episode source {source!r}; expected one of {sorted(EPISODE_SOURCES)}.")
        occurred = normalize_timestamp(reference_time, field="reference_time")
        recorded = normalize_timestamp(ingested_at, field="ingested_at")
        latest = self.latest_ingestion()
        if latest is not None and recorded < latest:
            raise ValueError("Episodes must be ingested in non-decreasing ingestion-time order.")

        block = self.memory.put(
            parent=self.structures.episodic_root,
            type=EPISODE_TYPE,
            value={
                "name": name,
                "content": content,
                "source": source,
                "source_description": source_description,
            },
            metadata={
                "name": name,
                "source": source,
                "reference_time": format_timestamp(occurred),
                "ingested_at": format_timestamp(recorded),
            },
        )
        try:
            self.structures.episode_time.add(block, occurred_at=occurred)
            item = self.structures.episodes.append(block)
        except Exception:
            self.memory.delete(block)
            raise
        return Episode(
            block=block,
            sequence=item.entry.sequence,
            reference_time=occurred,
            ingested_at=recorded,
        )

    def episodes(self, *, reverse: bool = False, limit: int = 50) -> list[Episode]:
        """Return episodes in ingestion order.

        Args:
            reverse: Whether to return the newest episodes first.
            limit: Maximum episodes to return.

        Returns:
            Episodes in ingestion order.
        """

        return [
            self._episode_from_block(item.block, sequence=item.entry.sequence)
            for item in self.structures.episodes.read(reverse=reverse, limit=limit)
        ]

    def episodes_between(
        self,
        *,
        start: datetime | None = None,
        stop: datetime | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> list[Episode]:
        """Return episodes whose reference time falls in a half-open range.

        Args:
            start: Optional inclusive lower bound on reference time.
            stop: Optional exclusive upper bound on reference time.
            limit: Maximum episodes to return.

        Returns:
            Matching episodes ordered by reference time then name.
        """

        items = self.structures.episode_time.events(
            start=normalize_timestamp(start, field="start") if start is not None else None,
            stop=normalize_timestamp(stop, field="stop") if stop is not None else None,
            limit=limit,
        )
        episodes = [self.get_episode(item.block) for item in items if item.block.type == EPISODE_TYPE]
        return sorted(episodes, key=lambda episode: (episode.reference_time, episode.name))

    def get_episode(self, episode: str | MemoryBlock | Episode) -> Episode:
        """Resolve an episode reference into a full episode record.

        Args:
            episode: Episode, block, or block id.

        Returns:
            The resolved episode.

        Raises:
            TypeError: If the referenced block is not an episode.
            ValueError: If the block or its log entry is missing.
        """

        if isinstance(episode, Episode):
            return episode
        block = self._require_type(episode, EPISODE_TYPE, role="episode")
        return self._episode_from_block(block)

    def latest_ingestion(self) -> datetime | None:
        """Return the ingestion time of the newest episode, if any.

        Returns:
            The newest ingestion timestamp, or `None` for an empty graph.
        """

        newest = self.structures.episodes.read(reverse=True, limit=1)
        if not newest:
            return None
        return parse_timestamp(
            required_metadata(newest[0].block, "ingested_at"),
            field="ingested_at",
        )

    def mentioned_entities(self, episode: str | MemoryBlock | Episode) -> list[MemoryBlock]:
        """Return the entity nodes an episode mentions.

        Args:
            episode: Episode, block, or block id.

        Returns:
            Entity blocks in mention order.
        """

        block = self.get_episode(episode).block
        return [
            neighbor.block
            for neighbor in self.structures.graph.neighbors(block, relation=MENTIONS_RELATION)
        ]

    def facts_from_episode(self, episode: str | MemoryBlock | Episode) -> list[TemporalFact]:
        """Return facts that cite an episode as provenance.

        Args:
            episode: Episode, block, or block id.

        Returns:
            Facts supported by that episode, ordered by valid time.
        """

        block = self.get_episode(episode).block
        facts = [
            self.resolve_fact(neighbor.block)
            for neighbor in self.structures.graph.neighbors(
                block,
                relation=PROVENANCE_RELATION,
                direction="in",
            )
        ]
        return sorted(facts, key=lambda fact: (fact.valid_at, fact.statement))

    # -- Semantic tier: entities and summaries ---------------------------

    def resolve_entity(self, name: str, *, entity_type: str, summary: str | None = None) -> Entity:
        """Return the entity for a type and name, creating it when unseen.

        Keyed identity is this lamp's deterministic stand-in for Graphiti's
        entity resolution: names are matched case-insensitively inside their
        type instead of by embedding similarity.

        Args:
            name: Display name of the entity.
            entity_type: Application-defined entity type.
            summary: Optional first summary. A short generated sentence is
                stored when omitted.

        Returns:
            The existing or newly created entity.

        Raises:
            ValueError: If the name is empty or the type is not allowed by a
                prescribed ontology.
        """

        require_text(name, field="Entity name")
        self.ontology.require_entity_type(entity_type)
        address = entity_address(name, entity_type=entity_type)
        existing = self.structures.entities.identity(address)
        if existing is not None:
            return self._entity_from_block(existing, address=address)

        block = self.memory.put(
            parent=self.structures.semantic_root,
            type=ENTITY_TYPE,
            value={"name": name, "entity_type": entity_type},
            metadata={"name": name, "entity_type": entity_type},
        )
        try:
            self.structures.entities.register(address, block)
            self._append_summary(
                block,
                address,
                summary if summary is not None else f"{name} is a {entity_type}.",
                recorded_at=self.latest_ingestion(),
            )
        except Exception:
            self.memory.delete(block)
            raise
        return self._entity_from_block(block, address=address)

    def get_entity(self, name: str, *, entity_type: str | None = None) -> Entity | None:
        """Look up one entity by name, optionally inside a single type.

        Args:
            name: Display name of the entity.
            entity_type: Optional type to disambiguate the lookup.

        Returns:
            The matching entity, or `None` when no entity matches.

        Raises:
            ValueError: If the name matches entities of several types and no
                type was supplied.
        """

        require_text(name, field="Entity name")
        if entity_type is not None:
            address = entity_address(name, entity_type=entity_type)
            block = self.structures.entities.identity(address)
            return None if block is None else self._entity_from_block(block, address=address)

        matches = [
            entity
            for entity in self.entities()
            if entity.name.casefold() == name.strip().casefold()
        ]
        if not matches:
            return None
        if len(matches) > 1:
            types = sorted(entity.entity_type for entity in matches)
            raise ValueError(f"Entity name {name!r} is ambiguous across types {types}.")
        return matches[0]

    def entity_of(self, entity: str | MemoryBlock | Entity) -> Entity:
        """Resolve an entity reference into a full entity record.

        Args:
            entity: Entity, entity block, or entity block id.

        Returns:
            The resolved entity with its current summary.

        Raises:
            TypeError: If the referenced block is not an entity.
            ValueError: If the block is missing.
        """

        if isinstance(entity, Entity):
            return entity
        block = self._require_type(entity, ENTITY_TYPE, role="entity")
        address = entity_address(
            required_string(block.value, "name"),
            entity_type=required_string(block.value, "entity_type"),
        )
        return self._entity_from_block(block, address=address)

    def entities(self, *, entity_type: str | None = None, limit: int = SEARCH_LIMIT) -> list[Entity]:
        """Return entity nodes in stable address order.

        Args:
            entity_type: Optional type whose namespace should be listed.
            limit: Maximum entities to return.

        Returns:
            Entities ordered by type then normalized name.
        """

        namespace = (ENTITY_NAMESPACE, entity_type) if entity_type is not None else (ENTITY_NAMESPACE,)
        return [
            self._entity_from_block(item.block, address=item.binding.address)
            for item in self.structures.entities.identities(
                namespace=namespace,
                recursive=entity_type is None,
                limit=limit,
            )
        ]

    def update_entity_summary(
        self,
        entity: str | MemoryBlock | Entity,
        summary: str,
        *,
        recorded_at: datetime | None = None,
    ) -> Entity:
        """Append a new immutable summary revision for an entity.

        Graphiti refreshes an entity summary whenever a new episode adds
        information about it, keeping the earlier text. Here each revision is
        its own block in the entity's revision log, so the whole history stays
        readable.

        Args:
            entity: Entity, block, or block id.
            summary: New summary text.
            recorded_at: Optional ingestion time for the revision. Defaults to
                the newest episode's ingestion time.

        Returns:
            The entity with its new current summary.

        Raises:
            ValueError: If the summary is empty or the entity is unregistered.
        """

        require_text(summary, field="Entity summary")
        resolved = self.entity_of(entity)
        self._append_summary(
            resolved.block,
            resolved.address,
            summary,
            recorded_at=recorded_at if recorded_at is not None else self.latest_ingestion(),
        )
        return self._entity_from_block(resolved.block, address=resolved.address)

    def summary_history(self, entity: str | MemoryBlock | Entity) -> list[SummaryRevision]:
        """Return every summary revision recorded for an entity.

        Args:
            entity: Entity, block, or block id.

        Returns:
            Revisions in ascending revision order.
        """

        resolved = self.entity_of(entity)
        return [
            SummaryRevision(
                block=item.block,
                revision=item.entry.revision,
                recorded_at=parse_timestamp(
                    required_metadata(item.block, "recorded_at"),
                    field="recorded_at",
                ),
            )
            for item in self.structures.entities.history(resolved.address, limit=SEARCH_LIMIT)
        ]

    # -- Semantic tier: facts --------------------------------------------

    def add_triplet(
        self,
        *,
        subject: str | MemoryBlock | Entity,
        relation: str,
        object: str | MemoryBlock | Entity,
        episode: str | MemoryBlock | Episode,
        valid_at: datetime,
        invalid_at: datetime | None = None,
        subject_type: str | None = None,
        object_type: str | None = None,
        fact: str | None = None,
    ) -> TripletResult:
        """Write one relationship fact extracted from an episode.

        The write follows Graphiti's edge-resolution order. A triplet already
        on record gains the new episode as extra provenance. Otherwise, when
        the ontology declares the relation single-valued, the subject's active
        fact for that relation is closed on both time axes and linked to the
        replacement, and only then is the new fact stored.

        Args:
            subject: Subject entity, block, or block id, or an entity name
                when `subject_type` is also supplied.
            relation: Application-defined relationship name.
            object: Object entity, block, or block id, or an entity name when
                `object_type` is also supplied.
            episode: Episode that supports the fact.
            valid_at: When the fact became true in the world.
            invalid_at: Optional time the fact stopped being true.
            subject_type: Entity type that makes `subject` a name to resolve.
            object_type: Entity type that makes `object` a name to resolve.
            fact: Optional sentence stored on the fact. A sentence is derived
                from the triplet when omitted.

        Returns:
            The fact now on record, plus anything this write invalidated.

        Raises:
            TypeError: If a referenced block has the wrong type.
            ValueError: If the ontology rejects the triplet, a timestamp is
                invalid, or the write would close an interval backwards.
        """

        subject_entity = self._as_entity(subject, entity_type=subject_type, role="subject")
        object_entity = self._as_entity(object, entity_type=object_type, role="object")
        self.ontology.require_edge(
            relation,
            subject_type=subject_entity.entity_type,
            object_type=object_entity.entity_type,
        )
        source = self.get_episode(episode)
        start = normalize_timestamp(valid_at, field="valid_at")
        end = normalize_timestamp(invalid_at, field="invalid_at") if invalid_at is not None else None
        if end is not None and end <= start:
            raise ValueError("invalid_at must be later than valid_at.")

        on_record = [
            candidate
            for candidate in self.history(subject_entity, relation=relation)
            if candidate.expired_at is None
        ]
        for candidate in on_record:
            if candidate.object.id == object_entity.id:
                self._add_provenance(candidate.block, source.block)
                self._add_mentions(source.block, (subject_entity.block, object_entity.block))
                return TripletResult(
                    fact=self.resolve_fact(candidate.block),
                    invalidated=(),
                    created=False,
                )

        superseded: list[TemporalFact] = []
        if self.ontology.is_single_valued(relation):
            for candidate in on_record:
                if candidate.invalid_at is not None and candidate.invalid_at <= start:
                    continue
                superseded.append(self._invalidate(candidate, at=start, ingested_at=source.ingested_at))
                self._add_provenance(candidate.block, source.block)

        sentence = fact if fact is not None else default_sentence(subject_entity, relation, object_entity)
        fact_block = self.memory.put(
            parent=self.structures.semantic_root,
            type=FACT_TYPE,
            value={
                "fact": sentence,
                "relation": relation,
                "subject_id": subject_entity.id,
                "object_id": object_entity.id,
            },
            metadata={
                "relation": relation,
                "subject_id": subject_entity.id,
                "object_id": object_entity.id,
                "subject_name": subject_entity.name,
                "object_name": object_entity.name,
            },
        )
        try:
            self.structures.valid_time.add(fact_block, validity=ValidityInterval(start, end))
            self.structures.transaction_time.add(
                fact_block,
                validity=ValidityInterval(source.ingested_at, None),
            )
            self.structures.graph.add_edge(fact_block, subject_entity.block, SUBJECT_RELATION)
            self.structures.graph.add_edge(fact_block, object_entity.block, OBJECT_RELATION)
            self._add_provenance(fact_block, source.block)
            self._add_mentions(source.block, (subject_entity.block, object_entity.block))
            for previous in superseded:
                self.structures.succession.add(previous.block, fact_block)
        except Exception:
            self.memory.delete(fact_block)
            raise
        return TripletResult(
            fact=self.resolve_fact(fact_block),
            invalidated=tuple(superseded),
            created=True,
        )

    def facts(
        self,
        *,
        subject: str | MemoryBlock | Entity | None = None,
        object: str | MemoryBlock | Entity | None = None,
        relation: str | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> list[TemporalFact]:
        """Return facts matching portable metadata filters.

        Args:
            subject: Optional subject entity, block, or id.
            object: Optional object entity, block, or id.
            relation: Optional relation name.
            limit: Maximum facts to return.

        Returns:
            Facts ordered by relation, statement, and valid time.
        """

        filters: dict[str, Any] = {}
        if subject is not None:
            filters["metadata.subject_id"] = self.entity_of(subject).id
        if object is not None:
            filters["metadata.object_id"] = self.entity_of(object).id
        if relation is not None:
            filters["metadata.relation"] = relation
        results = self.memory.search(types=[FACT_TYPE], filters=filters, limit=limit)
        return sorted(
            (self.resolve_fact(result.block) for result in results),
            key=lambda item: (item.relation, item.statement, item.valid_at),
        )

    def facts_at(
        self,
        when: datetime,
        *,
        known_at: datetime | None = None,
        subject: str | MemoryBlock | Entity | None = None,
        relation: str | None = None,
    ) -> list[TemporalFact]:
        """Return facts on a bi-temporal slice of the graph.

        With `known_at` omitted this answers "what is true at `when`, as best
        we know now". With `known_at` supplied it answers Graphiti's harder
        question: "what did the graph believe at `known_at` about `when`".
        Each axis is one `TemporalCollection` slice, and the answer is their
        intersection.

        Args:
            when: Effective time the fact must have been true at.
            known_at: Optional ingestion time the fact must have been on
                record at.
            subject: Optional subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            Matching facts ordered by relation, statement, and valid time.
        """

        instant = normalize_timestamp(when, field="when")
        valid_ids = {
            item.block.id
            for item in self.structures.valid_time.valid_at(instant, limit=SEARCH_LIMIT)
            if item.block.type == FACT_TYPE
        }
        if known_at is not None:
            recorded = normalize_timestamp(known_at, field="known_at")
            valid_ids &= {
                item.block.id
                for item in self.structures.transaction_time.valid_at(recorded, limit=SEARCH_LIMIT)
                if item.block.type == FACT_TYPE
            }
        subject_id = self.entity_of(subject).id if subject is not None else None
        selected = []
        for fact in (self.resolve_fact(block_id) for block_id in sorted(valid_ids)):
            if subject_id is not None and fact.subject.id != subject_id:
                continue
            if relation is not None and fact.relation != relation:
                continue
            selected.append(fact)
        return sorted(selected, key=lambda item: (item.relation, item.statement, item.valid_at))

    def facts_known_at(
        self,
        known_at: datetime,
        *,
        subject: str | MemoryBlock | Entity | None = None,
        relation: str | None = None,
    ) -> list[TemporalFact]:
        """Return the facts this graph held on record at an ingestion time.

        This is the transaction-time axis on its own: it answers "what had the
        graph been told by then", regardless of when those facts were true.
        Comparing it with `facts_at` exposes the ingestion lag Graphiti's
        bi-temporal model exists to make visible.

        Args:
            known_at: Ingestion time the fact must have been on record at.
            subject: Optional subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            Matching facts ordered by relation, statement, and valid time.
        """

        recorded = normalize_timestamp(known_at, field="known_at")
        subject_id = self.entity_of(subject).id if subject is not None else None
        selected = []
        for item in self.structures.transaction_time.valid_at(recorded, limit=SEARCH_LIMIT):
            if item.block.type != FACT_TYPE:
                continue
            fact = self.resolve_fact(item.block)
            if subject_id is not None and fact.subject.id != subject_id:
                continue
            if relation is not None and fact.relation != relation:
                continue
            selected.append(fact)
        return sorted(selected, key=lambda item: (item.relation, item.statement, item.valid_at))

    def neighborhood(
        self,
        entity: str | MemoryBlock | Entity,
        *,
        as_of: datetime,
        known_at: datetime | None = None,
    ) -> list[TemporalFact]:
        """Return facts touching one entity on a bi-temporal slice.

        Args:
            entity: Entity, block, or id at the center of the neighborhood.
            as_of: Effective time the facts must have been true at.
            known_at: Optional ingestion time the facts must have been on
                record at.

        Returns:
            Incoming and outgoing facts in deterministic order.
        """

        center = self.entity_of(entity)
        return [
            fact
            for fact in self.facts_at(as_of, known_at=known_at)
            if center.id in {fact.subject.id, fact.object.id}
        ]

    def history(
        self,
        subject: str | MemoryBlock | Entity,
        *,
        relation: str | None = None,
    ) -> list[TemporalFact]:
        """Return the full recorded history of a subject's relationship.

        Args:
            subject: Subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            Facts ordered by valid-time start, including invalidated ones.
        """

        return sorted(
            self.facts(subject=subject, relation=relation),
            key=lambda fact: (fact.valid_at, fact.statement),
        )

    def provenance(self, fact: TemporalFact | MemoryBlock | str) -> list[Episode]:
        """Return the episodes that support a fact.

        The episode that invalidated a fact is included, because it is the
        evidence for closing that fact's intervals.

        Args:
            fact: Fact, block, or block id.

        Returns:
            Episodes in link insertion order.
        """

        block = fact.block if isinstance(fact, TemporalFact) else self._require_type(fact, FACT_TYPE, role="fact")
        return [
            self.get_episode(neighbor.block)
            for neighbor in self.structures.graph.neighbors(block, relation=PROVENANCE_RELATION)
        ]

    def superseded_by(self, fact: TemporalFact | MemoryBlock | str) -> TemporalFact | None:
        """Return the fact that replaced a closed fact, if any.

        Args:
            fact: Fact, block, or block id.

        Returns:
            The replacement fact, or `None` when the fact was never replaced.
        """

        block = fact.block if isinstance(fact, TemporalFact) else self._require_type(fact, FACT_TYPE, role="fact")
        successors = self.structures.succession.successors(block)
        return self.resolve_fact(successors[0].block) if successors else None

    def resolve_fact(self, fact: str | MemoryBlock | TemporalFact) -> TemporalFact:
        """Resolve a fact reference into its entities and both intervals.

        Args:
            fact: Fact, block, or block id.

        Returns:
            The resolved fact.

        Raises:
            TypeError: If the referenced block is not a fact.
            ValueError: If either time axis is missing an interval.
        """

        if isinstance(fact, TemporalFact):
            return fact
        block = self._require_type(fact, FACT_TYPE, role="fact")
        return TemporalFact(
            block=block,
            subject=self._single_neighbor(block, SUBJECT_RELATION),
            object=self._single_neighbor(block, OBJECT_RELATION),
            valid=self._interval(self.structures.valid_time, block, axis="valid time"),
            recorded=self._interval(self.structures.transaction_time, block, axis="transaction time"),
        )

    # -- Internals -------------------------------------------------------

    def _invalidate(
        self,
        fact: TemporalFact,
        *,
        at: datetime,
        ingested_at: datetime,
    ) -> TemporalFact:
        """Close both time axes of a contradicted fact."""

        if at <= fact.valid_at:
            raise ValueError(
                "Single-valued facts must be asserted in effective-time order; "
                f"{fact.statement!r} is already valid from {format_timestamp(fact.valid_at)}."
            )
        if ingested_at <= fact.ingested_at:
            raise ValueError(
                "A contradicting fact must be ingested after the fact it supersedes; "
                f"{fact.statement!r} was recorded at {format_timestamp(fact.ingested_at)}."
            )
        self.structures.valid_time.replace(
            fact.block,
            validity=ValidityInterval(fact.valid_at, at),
        )
        self.structures.transaction_time.replace(
            fact.block,
            validity=ValidityInterval(fact.ingested_at, ingested_at),
        )
        return self.resolve_fact(fact.block)

    def _append_summary(
        self,
        entity: MemoryBlock,
        address: MemoryAddress,
        summary: str,
        *,
        recorded_at: datetime | None,
    ) -> None:
        """Store one summary block and append it to the entity's log."""

        stamp = recorded_at if recorded_at is not None else EPOCH
        block = self.memory.put(
            parent=entity,
            type=SUMMARY_TYPE,
            value={"summary": summary},
            metadata={
                "entity_id": entity.id,
                "recorded_at": format_timestamp(stamp),
            },
        )
        try:
            self.structures.entities.append(address, block)
        except Exception:
            self.memory.delete(block)
            raise

    def _add_provenance(self, fact: MemoryBlock, episode: MemoryBlock) -> None:
        """Link a fact to a supporting episode at most once."""

        edges = self.structures.graph.edges(fact, relation=PROVENANCE_RELATION)
        if not any(edge.target_id == episode.id for edge in edges):
            self.structures.graph.add_edge(fact, episode, PROVENANCE_RELATION)

    def _add_mentions(self, episode: MemoryBlock, entities: Iterable[MemoryBlock]) -> None:
        """Link an episode to the entities it mentions at most once each."""

        linked = {
            edge.target_id
            for edge in self.structures.graph.edges(episode, relation=MENTIONS_RELATION)
        }
        for entity in entities:
            if entity.id not in linked:
                self.structures.graph.add_edge(episode, entity, MENTIONS_RELATION)
                linked.add(entity.id)

    def _episode_from_block(self, block: MemoryBlock, *, sequence: int | None = None) -> Episode:
        """Build an episode record from its block and log position."""

        if sequence is None:
            item = self.structures.episodes.read(limit=SEARCH_LIMIT)
            found = next((entry for entry in item if entry.block.id == block.id), None)
            if found is None:
                raise ValueError(f"Episode {block.id!r} is not in the ingestion log.")
            sequence = found.entry.sequence
        return Episode(
            block=block,
            sequence=sequence,
            reference_time=parse_timestamp(
                required_metadata(block, "reference_time"),
                field="reference_time",
            ),
            ingested_at=parse_timestamp(
                required_metadata(block, "ingested_at"),
                field="ingested_at",
            ),
        )

    def _entity_from_block(self, block: MemoryBlock, *, address: MemoryAddress) -> Entity:
        """Build an entity record with its newest summary revision."""

        current = self.structures.entities.current(address)
        return Entity(
            block=block,
            address=address,
            summary=required_string(current.block.value, "summary") if current is not None else "",
            revision=current.entry.revision if current is not None else 0,
        )

    def _as_entity(
        self,
        entity: str | MemoryBlock | Entity,
        *,
        entity_type: str | None,
        role: str,
    ) -> Entity:
        """Resolve a triplet endpoint, creating it when given as a name."""

        del role
        if isinstance(entity, str) and entity_type is not None:
            return self.resolve_entity(entity, entity_type=entity_type)
        return self.entity_of(entity)

    def _require_type(self, block: str | MemoryBlock, expected: str, *, role: str) -> MemoryBlock:
        """Fetch a visible block and validate its application-defined type."""

        block_id = block.id if isinstance(block, MemoryBlock) else block
        stored = self.memory.get(block_id)
        if stored is None:
            raise ValueError(f"Unknown {role} block {block_id!r}.")
        if stored.type != expected:
            raise TypeError(f"Expected {role} block type {expected!r}, got {stored.type!r}.")
        return stored

    def _single_neighbor(self, block: MemoryBlock, relation: str) -> MemoryBlock:
        """Resolve exactly one outgoing structural edge for a fact role."""

        neighbors = self.structures.graph.neighbors(block, relation=relation)
        if len(neighbors) != 1:
            raise ValueError(f"Fact {block.id!r} must have exactly one outgoing {relation!r} edge.")
        return neighbors[0].block

    def _interval(
        self,
        collection: TemporalCollection,
        block: MemoryBlock,
        *,
        axis: str,
    ) -> ValidityInterval:
        """Read one time axis for a fact block."""

        item = collection.get(block)
        if item is None or item.entry.validity is None:
            raise ValueError(f"Fact {block.id!r} has no {axis} interval.")
        return item.entry.validity


