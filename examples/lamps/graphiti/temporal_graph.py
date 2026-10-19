"""The bi-temporal knowledge graph behavior of the Graphiti lamp.

Graphiti ingests a stream of episodes and extracts entities and relationship
facts from them.

- **Bi-temporal timeline:** 2 timestamps tell when a fact was true in the
  world, and 2 more tell when the system believed it.
- **Invalidation, not deletion:** the graph invalidates a contradicted fact and
  does not delete it. So the graph can answer "what was true then?" and also
  "what did we know then?".
- **Rules in the lamp:** this module owns these rules. Genie supplies blocks,
  ownership, scoped access, links, search, and the structure facades.
- **Meaning in the lamp:** the graph class decides what an episode, entity,
  fact, provenance edge, mention, and validity interval mean. It also decides
  the order in which a write resolves entities, removes duplicates, and
  invalidates facts.
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
    """Bi-temporal graph over a bounded Genie handle, based on Graphiti.

    - **Ownership:** the graph owns every episode, entity, summary revision,
      fact, and community below one root.
    - **Rules in the lamp:** all rules for taxonomy, contradiction, and time are
      in this class. Genie only stores blocks, links, and structure membership.
    - **Partition:** one graph partition is the same as one Graphiti
      `group_id`. Separate roots with separate handles cannot see the nodes of
      each other. So the handle gives the namespace, and no record needs a
      namespace field.
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

        - **Logs:** the episode joins the ingestion event log. Its reference
          time becomes its event time.
        - **Order:** ingestion times cannot move backwards, because transaction
          time is the clock that closes superseded facts. The method accepts an
          `ingested_at` that is equal to the newest ingestion time.

        Args:
            name: Short episode name.
            content: Raw episode body.
            reference_time: Time of the source event.
            ingested_at: Time when this graph processed the episode.
            source: Episode kind. It is the same as `EpisodeType` in Graphiti.
            source_description: Free-text description of the origin.

        Returns:
            The stored episode.

        Raises:
            TypeError: If `name` or `content` is not a string, or a timestamp is
                not a `datetime`.
            ValueError: If `name` or `content` is empty, a timestamp has no
                timezone, the source kind is unknown, or `ingested_at` is
                earlier than the ingestion time of the newest episode.
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
            reverse: If `True`, return the newest episodes first.
            limit: Maximum number of episodes to return.

        Returns:
            Episodes in ingestion order, or in the opposite order if `reverse`
            is `True`.
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
        """Return the episodes with a reference time in a half-open range.

        Args:
            start: Optional inclusive lower bound on reference time.
            stop: Optional exclusive upper bound on reference time.
            limit: Maximum number of episodes to return.

        Returns:
            The episodes that match, in order of reference time and then name.
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
            TypeError: If the block is not an episode.
            ValueError: If the block or its log entry does not exist.
        """

        if isinstance(episode, Episode):
            return episode
        block = self._require_type(episode, EPISODE_TYPE, role="episode")
        return self._episode_from_block(block)

    def latest_ingestion(self) -> datetime | None:
        """Return the ingestion time of the newest episode, if there is one.

        Returns:
            The newest ingestion timestamp, or `None` if the graph has no
            episodes.
        """

        newest = self.structures.episodes.read(reverse=True, limit=1)
        if not newest:
            return None
        return parse_timestamp(
            required_metadata(newest[0].block, "ingested_at"),
            field="ingested_at",
        )

    def mentioned_entities(self, episode: str | MemoryBlock | Episode) -> list[MemoryBlock]:
        """Return the entity nodes that an episode mentions.

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
            Facts that the episode supports, in order of valid time and then
            statement.
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
        """Return the entity for a type and name, and create it if it is new.

        Keyed identity is the deterministic replacement in this lamp for entity
        resolution in Graphiti. Names match without case sensitivity inside
        their type, not by embedding similarity.

        Args:
            name: Display name of the entity.
            entity_type: Application-defined entity type.
            summary: Optional first summary. If it is `None`, the graph stores
                a short generated sentence. The graph ignores this value for an
                existing entity.

        Returns:
            The existing entity or the new entity.

        Raises:
            ValueError: If the name is empty, or a prescribed ontology does not
                allow the type.
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
        """Find one entity by name, and optionally search only inside one type.

        Args:
            name: Display name of the entity.
            entity_type: Optional type. Use it when entities of several types
                have the same name.

        Returns:
            The entity that matches, or `None` if no entity matches.

        Raises:
            ValueError: If the name is empty, or if the name matches entities of
                several types and the call gives no type.
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
            TypeError: If the block is not an entity.
            ValueError: If the block does not exist.
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
            entity_type: Optional type. If you give it, the method lists only
                the namespace of this type.
            limit: Maximum number of entities to return.

        Returns:
            Entities in order of type and then normalized name.
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

        - **Upstream:** Graphiti updates an entity summary each time a new
          episode adds information about the entity. It keeps the earlier text.
        - **Here:** each revision is a separate block in the revision log of the
          entity. So the full history stays readable.

        Args:
            entity: Entity, block, or block id.
            summary: New summary text.
            recorded_at: Optional ingestion time for the revision. The default
                is the ingestion time of the newest episode. If the graph has
                no episodes, the revision uses `EPOCH`.

        Returns:
            The entity with its new current summary.

        Raises:
            ValueError: If the summary is empty or the entity block does not
                exist.
            MemoryNotFound: If the entity block is not registered as an
                identity in `VersionedRecords`.
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
        """Return every summary revision that the graph recorded for an entity.

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

        The write follows the edge-resolution order of Graphiti:

        - **Repeated triplet:** if the triplet is already on record, the fact
          gets the new episode as extra provenance. The write stores no new
          fact.
        - **Single-valued relation:** if the ontology declares the relation
          single-valued, the write closes the active facts of the subject for
          that relation on both time axes. It does not close a fact whose valid
          time ended at or before `valid_at`. Each closed fact also gets the
          new episode as provenance.
        - **New fact:** after that, the write stores the new fact. Then it links
          each closed fact to the new fact with a succession edge.

        Args:
            subject: Subject entity, block, or block id. It can also be an
                entity name if you also give `subject_type`.
            relation: Application-defined relationship name.
            object: Object entity, block, or block id. It can also be an entity
                name if you also give `object_type`.
            episode: Episode that supports the fact.
            valid_at: Time when the fact became true in the world.
            invalid_at: Optional time when the fact was no longer true.
            subject_type: Entity type. If you give it, the graph resolves
                `subject` as a name and creates the entity if it does not exist.
            object_type: Entity type. If you give it, the graph resolves
                `object` as a name and creates the entity if it does not exist.
            fact: Optional sentence to store on the fact. If you omit it, the
                graph makes a sentence from the triplet.

        Returns:
            The fact that is now on record, and the facts that this write
            invalidated.

        Raises:
            TypeError: If a block has the wrong type.
            ValueError: If the ontology rejects the triplet, a timestamp is
                invalid, `invalid_at` is not later than `valid_at`, or the
                write would close an interval at or before its start.
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
        """Return the facts that match portable metadata filters.

        Args:
            subject: Optional subject entity, block, or id.
            object: Optional object entity, block, or id.
            relation: Optional relation name.
            limit: Maximum number of facts to return.

        Returns:
            Facts in order of relation, statement, and valid time.
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

        - **Without `known_at`:** the method answers "what is true at `when`, as
          far as we know now?".
        - **With `known_at`:** the method answers the harder Graphiti question:
          "what did the graph believe at `known_at` about `when`?".
        - **How:** each axis is one `TemporalCollection` slice. The answer is
          the intersection of the 2 slices.

        Args:
            when: Effective time. The method returns only facts that were true
                at this time.
            known_at: Optional ingestion time. If you give it, the method
                returns only facts that were on record at this time.
            subject: Optional subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            The facts that match, in order of relation, statement, and valid
            time.
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
        """Return the facts that this graph held on record at an ingestion time.

        - **One axis:** this is the transaction-time axis alone. It answers
          "what did the graph know at that time?", and it ignores when those
          facts were true.
        - **Ingestion lag:** a comparison with `facts_at` shows the ingestion
          lag. The bi-temporal model of Graphiti exists to make this lag
          visible.

        Args:
            known_at: Ingestion time. The method returns only facts that were on
                record at this time.
            subject: Optional subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            The facts that match, in order of relation, statement, and valid
            time.
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
        """Return the facts on a bi-temporal slice that connect to one entity.

        Args:
            entity: Entity, block, or id at the center of the neighborhood.
            as_of: Effective time. The method returns only facts that were true
                at this time.
            known_at: Optional ingestion time. If you give it, the method
                returns only facts that were on record at this time.

        Returns:
            Facts with the entity as subject or as object, in deterministic
            order.
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
        """Return the full recorded history of the relationships of a subject.

        Args:
            subject: Subject entity, block, or id.
            relation: Optional relation name.

        Returns:
            Facts in order of valid-time start and then statement. The list
            includes invalidated facts.
        """

        return sorted(
            self.facts(subject=subject, relation=relation),
            key=lambda fact: (fact.valid_at, fact.statement),
        )

    def provenance(self, fact: TemporalFact | MemoryBlock | str) -> list[Episode]:
        """Return the episodes that support a fact.

        The list includes the episode that invalidated the fact, because that
        episode is the evidence for the close of the fact intervals.

        Args:
            fact: Fact, block, or block id.

        Returns:
            Episodes in the order in which the graph added the links.
        """

        block = fact.block if isinstance(fact, TemporalFact) else self._require_type(fact, FACT_TYPE, role="fact")
        return [
            self.get_episode(neighbor.block)
            for neighbor in self.structures.graph.neighbors(block, relation=PROVENANCE_RELATION)
        ]

    def superseded_by(self, fact: TemporalFact | MemoryBlock | str) -> TemporalFact | None:
        """Return the fact that replaced a closed fact, if there is one.

        Args:
            fact: Fact, block, or block id.

        Returns:
            The replacement fact, or `None` if no fact replaced it.
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
            TypeError: If the block is not a fact.
            ValueError: If the block does not exist, a time axis has no interval
                for the fact, or the fact does not have exactly one subject edge
                and one object edge.
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
        """Store one summary block and append it to the log of the entity."""

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
        """Link a fact to an episode that supports it, without duplicates."""

        edges = self.structures.graph.edges(fact, relation=PROVENANCE_RELATION)
        if not any(edge.target_id == episode.id for edge in edges):
            self.structures.graph.add_edge(fact, episode, PROVENANCE_RELATION)

    def _add_mentions(self, episode: MemoryBlock, entities: Iterable[MemoryBlock]) -> None:
        """Link an episode to each mentioned entity, without duplicates."""

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
        """Resolve a triplet endpoint. Create the entity for a new typed name."""

        del role
        if isinstance(entity, str) and entity_type is not None:
            return self.resolve_entity(entity, entity_type=entity_type)
        return self.entity_of(entity)

    def _require_type(self, block: str | MemoryBlock, expected: str, *, role: str) -> MemoryBlock:
        """Read a visible block and validate its application-defined type."""

        block_id = block.id if isinstance(block, MemoryBlock) else block
        stored = self.memory.get(block_id)
        if stored is None:
            raise ValueError(f"Unknown {role} block {block_id!r}.")
        if stored.type != expected:
            raise TypeError(f"Expected {role} block type {expected!r}, got {stored.type!r}.")
        return stored

    def _single_neighbor(self, block: MemoryBlock, relation: str) -> MemoryBlock:
        """Resolve exactly one structural edge from a fact for a fact role."""

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


