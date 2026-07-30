"""A small temporal knowledge graph assembled from Genie primitives.

This module deliberately keeps Graphiti-specific semantics in the example
layer. Genie supplies memory blocks, ownership, scoped access, search, and
typed links; this module decides what an entity, episode, fact, provenance
edge, and validity interval mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from genie import MemoryBlock, MemoryHandle

ENTITY_TYPE = "graphiti.entity"
EPISODE_TYPE = "graphiti.episode"
FACT_TYPE = "graphiti.temporal_fact"

SUBJECT_RELATION = "graphiti.subject"
OBJECT_RELATION = "graphiti.object"
PROVENANCE_RELATION = "graphiti.provenance"
SUPERSEDED_BY_RELATION = "graphiti.superseded_by"

_SEARCH_LIMIT = 1_000


@dataclass(frozen=True, slots=True)
class TemporalFact:
    """Resolved view of a temporal fact stored in Genie.

    Attributes:
        block: First-class memory block holding the fact payload and validity.
        subject: Entity at the source of the relationship.
        object: Entity at the target of the relationship.
    """

    block: MemoryBlock
    subject: MemoryBlock
    object: MemoryBlock

    @property
    def relation(self) -> str:
        """Return the application-defined relationship name."""

        return _required_string(self.block.value, "relation")

    @property
    def valid_from(self) -> datetime:
        """Return the inclusive start of the fact's validity interval."""

        return _parse_timestamp(_required_string(self.block.metadata, "valid_from"))

    @property
    def valid_to(self) -> datetime | None:
        """Return the exclusive interval end, or ``None`` when still open."""

        value = self.block.metadata.get("valid_to")
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("Temporal fact metadata 'valid_to' must be a string or None.")
        return _parse_timestamp(value)

    @property
    def statement(self) -> str:
        """Return a readable representation of the resolved relationship."""

        return f"{_entity_name(self.subject)} -[{self.relation}]-> {_entity_name(self.object)}"

    def is_valid_at(self, when: datetime) -> bool:
        """Return whether this fact is valid at a timezone-aware instant.

        Validity intervals are half-open: ``[valid_from, valid_to)``.

        Args:
            when: Effective time to check.

        Returns:
            ``True`` when ``when`` falls inside the fact's validity interval.
        """

        instant = _normalize_timestamp(when, field="when")
        return self.valid_from <= instant and (self.valid_to is None or instant < self.valid_to)


class TemporalKnowledgeGraph:
    """Graphiti-inspired application model backed by a scoped Genie handle.

    Entities, source episodes, and temporal facts are blocks owned by the
    handle root. Typed links connect facts to their subject, object, and source
    episodes. A fact stores its effective-time interval in metadata so it can
    be updated independently of the entities it relates.
    """

    def __init__(self, memory: MemoryHandle) -> None:
        """Create a graph within an existing bounded memory handle.

        Args:
            memory: Handle rooted at the block that owns the whole graph.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("TemporalKnowledgeGraph requires a bounded MemoryHandle.")
        self.memory = memory

    def add_entity(self, name: str, *, entity_type: str) -> MemoryBlock:
        """Add an entity node to the graph.

        Args:
            name: Human-readable entity name.
            entity_type: Application-defined entity category.

        Returns:
            The stored entity block.
        """

        if not name:
            raise ValueError("Entity name cannot be empty.")
        if not entity_type:
            raise ValueError("Entity type cannot be empty.")
        return self.memory.put(
            type=ENTITY_TYPE,
            value={"name": name, "entity_type": entity_type},
            metadata={"name": name, "entity_type": entity_type},
        )

    def add_episode(
        self,
        content: str,
        *,
        source: str,
        observed_at: datetime,
    ) -> MemoryBlock:
        """Store a source episode that can provide provenance for facts.

        Args:
            content: Raw source text or event description.
            source: Source identifier, such as a chat or document name.
            observed_at: When the source event happened.

        Returns:
            The stored episode block.
        """

        if not content:
            raise ValueError("Episode content cannot be empty.")
        if not source:
            raise ValueError("Episode source cannot be empty.")
        return self.memory.put(
            type=EPISODE_TYPE,
            value={"content": content, "source": source},
            metadata={
                "source": source,
                "observed_at": _format_timestamp(
                    _normalize_timestamp(observed_at, field="observed_at")
                ),
            },
        )

    def assert_fact(
        self,
        subject: str | MemoryBlock,
        relation: str,
        object: str | MemoryBlock,
        *,
        episode: str | MemoryBlock,
        valid_from: datetime,
        valid_to: datetime | None = None,
        exclusive: bool = False,
    ) -> TemporalFact:
        """Assert a temporal relationship and attach its provenance.

        When ``exclusive`` is true, an active fact with the same subject and
        relation is closed at ``valid_from`` before the new fact is added. This
        models single-valued relationships such as ``lives_in``. Exclusive
        facts must be asserted in effective-time order. Reasserting the same
        active relationship adds provenance to the existing fact.

        Args:
            subject: Subject entity block or id.
            relation: Application-defined relationship name.
            object: Object entity block or id.
            episode: Source episode block or id.
            valid_from: Inclusive effective-time start.
            valid_to: Optional exclusive effective-time end.
            exclusive: Whether this assertion supersedes the active value for
                the same subject and relation.

        Returns:
            Resolved view of the stored or consolidated temporal fact.

        Raises:
            TypeError: If an endpoint is not an entity or provenance is not an
                episode.
            ValueError: If times are invalid or exclusive assertions arrive
                out of effective-time order.
        """

        if not relation:
            raise ValueError("Fact relation cannot be empty.")
        subject_block = self._require_block_type(subject, ENTITY_TYPE, role="subject")
        object_block = self._require_block_type(object, ENTITY_TYPE, role="object")
        episode_block = self._require_block_type(episode, EPISODE_TYPE, role="episode")
        start = _normalize_timestamp(valid_from, field="valid_from")
        end = (
            _normalize_timestamp(valid_to, field="valid_to")
            if valid_to is not None
            else None
        )
        if end is not None and end <= start:
            raise ValueError("valid_to must be later than valid_from.")

        history = self.history(subject_block, relation=relation)
        if exclusive and history and start < max(fact.valid_from for fact in history):
            raise ValueError("Exclusive facts must be asserted in effective-time order.")

        active_facts = [fact for fact in history if fact.is_valid_at(start)]
        for active in active_facts:
            if active.object.id == object_block.id:
                self._add_provenance(active.block, episode_block)
                return self.resolve_fact(active.block)

        superseded: list[TemporalFact] = []
        if exclusive:
            for active in active_facts:
                updated = self.memory.update(
                    active.block,
                    metadata={"valid_to": _format_timestamp(start)},
                )
                self._add_provenance(updated, episode_block)
                superseded.append(self.resolve_fact(updated))

        fact_block = self.memory.put(
            type=FACT_TYPE,
            value={
                "subject_id": subject_block.id,
                "relation": relation,
                "object_id": object_block.id,
            },
            metadata={
                "subject_id": subject_block.id,
                "relation": relation,
                "object_id": object_block.id,
                "valid_from": _format_timestamp(start),
                "valid_to": _format_timestamp(end) if end is not None else None,
            },
        )
        self.memory.add_link(fact_block, subject_block, SUBJECT_RELATION)
        self.memory.add_link(fact_block, object_block, OBJECT_RELATION)
        self._add_provenance(fact_block, episode_block)
        for previous in superseded:
            self.memory.add_link(previous.block, fact_block, SUPERSEDED_BY_RELATION)
        return self.resolve_fact(fact_block)

    def facts_at(
        self,
        when: datetime,
        *,
        subject: str | MemoryBlock | None = None,
        relation: str | None = None,
    ) -> list[TemporalFact]:
        """Return facts valid at an effective-time instant.

        Args:
            when: Timezone-aware effective time.
            subject: Optional subject entity block or id.
            relation: Optional relationship filter.

        Returns:
            Matching facts sorted by relationship and entity names.
        """

        instant = _normalize_timestamp(when, field="when")
        facts = self._search_facts(subject=subject, relation=relation)
        return sorted(
            (fact for fact in facts if fact.is_valid_at(instant)),
            key=lambda fact: (fact.relation, fact.statement, fact.valid_from),
        )

    def neighborhood_at(
        self,
        entity: str | MemoryBlock,
        when: datetime,
    ) -> list[TemporalFact]:
        """Return valid facts connected to an entity at one instant.

        This is a small graph-neighborhood query: the entity may be either the
        subject or the object of each returned fact.

        Args:
            entity: Entity block or id at the center of the neighborhood.
            when: Timezone-aware effective time.

        Returns:
            Valid incoming and outgoing facts in deterministic order.
        """

        entity_block = self._require_block_type(entity, ENTITY_TYPE, role="entity")
        return [
            fact
            for fact in self.facts_at(when)
            if entity_block.id in {fact.subject.id, fact.object.id}
        ]

    def history(
        self,
        subject: str | MemoryBlock,
        *,
        relation: str | None = None,
    ) -> list[TemporalFact]:
        """Return the effective-time history for one subject.

        Args:
            subject: Subject entity block or id.
            relation: Optional relationship filter.

        Returns:
            Facts ordered by validity start.
        """

        return sorted(
            self._search_facts(subject=subject, relation=relation),
            key=lambda fact: fact.valid_from,
        )

    def provenance(self, fact: TemporalFact | MemoryBlock) -> list[MemoryBlock]:
        """Return source episodes linked to a fact.

        The episode that supersedes a fact is included because it provides the
        evidence for closing that fact's validity interval.

        Args:
            fact: Temporal fact view or block.

        Returns:
            Episode blocks in link insertion order.
        """

        block = fact.block if isinstance(fact, TemporalFact) else fact
        self._require_block_type(block, FACT_TYPE, role="fact")
        episodes: list[MemoryBlock] = []
        for link in self.memory.list_links(block, relation=PROVENANCE_RELATION):
            episode = self.memory.get(link.target_id)
            if episode is not None:
                episodes.append(episode)
        return episodes

    def resolve_fact(self, fact: str | MemoryBlock) -> TemporalFact:
        """Resolve a fact block into its subject and object entity blocks.

        Args:
            fact: Fact block or id.

        Returns:
            A resolved temporal fact view.
        """

        fact_block = self._require_block_type(fact, FACT_TYPE, role="fact")
        subject = self._single_linked_block(fact_block, SUBJECT_RELATION)
        object_block = self._single_linked_block(fact_block, OBJECT_RELATION)
        return TemporalFact(block=fact_block, subject=subject, object=object_block)

    def _search_facts(
        self,
        *,
        subject: str | MemoryBlock | None,
        relation: str | None,
    ) -> list[TemporalFact]:
        """Search fact blocks using portable metadata filters."""

        filters: dict[str, Any] = {}
        if subject is not None:
            subject_block = self._require_block_type(subject, ENTITY_TYPE, role="subject")
            filters["metadata.subject_id"] = subject_block.id
        if relation is not None:
            filters["metadata.relation"] = relation
        results = self.memory.search(
            types=[FACT_TYPE],
            filters=filters,
            limit=_SEARCH_LIMIT,
        )
        return [self.resolve_fact(result.block) for result in results]

    def _add_provenance(self, fact: MemoryBlock, episode: MemoryBlock) -> None:
        """Link an episode once, avoiding duplicate provenance edges."""

        links = self.memory.list_links(fact, relation=PROVENANCE_RELATION)
        if not any(link.target_id == episode.id for link in links):
            self.memory.add_link(fact, episode, PROVENANCE_RELATION)

    def _single_linked_block(self, fact: MemoryBlock, relation: str) -> MemoryBlock:
        """Resolve exactly one outgoing link for a structural fact role."""

        links = self.memory.list_links(fact, relation=relation)
        if len(links) != 1:
            raise ValueError(
                f"Fact {fact.id!r} must have exactly one outgoing {relation!r} link."
            )
        block = self.memory.get(links[0].target_id)
        if block is None:
            raise ValueError(f"Fact {fact.id!r} links to a missing block.")
        return block

    def _require_block_type(
        self,
        block: str | MemoryBlock,
        expected_type: str,
        *,
        role: str,
    ) -> MemoryBlock:
        """Fetch a block and validate its application-defined type."""

        block_id = block.id if isinstance(block, MemoryBlock) else block
        stored = self.memory.get(block_id)
        if stored is None:
            raise ValueError(f"Unknown {role} block {block_id!r}.")
        if stored.type != expected_type:
            raise TypeError(
                f"Expected {role} block type {expected_type!r}, got {stored.type!r}."
            )
        return stored


def _normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Normalize a timezone-aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    """Format a UTC timestamp as a JSON-compatible ISO 8601 string."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    """Parse the ISO 8601 representation used in fact metadata."""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _required_string(mapping: Any, key: str) -> str:
    """Read a required string from a dictionary-like value."""

    if not isinstance(mapping, dict) or not isinstance(mapping.get(key), str):
        raise TypeError(f"Expected string field {key!r}.")
    return mapping[key]


def _entity_name(entity: MemoryBlock) -> str:
    """Return the name stored in an entity block."""

    return _required_string(entity.value, "name")
