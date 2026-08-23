"""Value types, constants, and time helpers for the Graphiti lamp.

Graphiti keeps its own node and edge models in dedicated modules; this module
plays the same role for the lamp. It holds the block types and relation names
the example writes, the timestamp helpers every layer shares, and the read-only
records the graph hands back: an ingested episode, an entity with its current
summary, one summary revision, a fact on both time axes, and the outcome of
writing a triplet.

Nothing here touches Genie beyond reading blocks that were already stored, so
every other module in the lamp can depend on it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from genie import MemoryBlock
from genie.structures import MemoryAddress, ValidityInterval

GRAPH_TYPE = "graphiti.graph"
EPISODE_TYPE = "graphiti.episode"
ENTITY_TYPE = "graphiti.entity"
SUMMARY_TYPE = "graphiti.entity_summary"
FACT_TYPE = "graphiti.fact"
COMMUNITY_TYPE = "graphiti.community"

SUBJECT_RELATION = "graphiti.subject"
OBJECT_RELATION = "graphiti.object"
PROVENANCE_RELATION = "graphiti.provenance"
MENTIONS_RELATION = "graphiti.mentions"
MEMBER_RELATION = "graphiti.has_member"
SUPERSEDED_BY_RELATION = "graphiti.superseded_by"

ENTITY_NAMESPACE = "entities"

EpisodeSource = Literal["message", "text", "json", "fact_triple"]
EPISODE_SOURCES: frozenset[str] = frozenset({"message", "text", "json", "fact_triple"})

ANCHOR_TYPE = "graphiti.anchor"
SEARCH_LIMIT = 1_000


def normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Return a timezone-aware timestamp normalized to UTC.

    Args:
        value: Timestamp supplied by application code.
        field: Field name used in error messages.

    Returns:
        The same instant expressed in UTC.

    Raises:
        TypeError: If `value` is not a `datetime`.
        ValueError: If `value` has no timezone.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Format a timestamp as a JSON-compatible ISO 8601 string in UTC.

    Args:
        value: Timezone-aware timestamp.

    Returns:
        An ISO 8601 string ending in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name used in error messages.

    Returns:
        The parsed instant in UTC.

    Raises:
        TypeError: If `value` is not a string.
        ValueError: If the string is not a timezone-aware ISO 8601 timestamp.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO 8601 string.")
    return normalize_timestamp(datetime.fromisoformat(value), field=field)


@dataclass(frozen=True, slots=True)
class Episode:
    """One ingested source record and its place on both time axes.

    An episode is Graphiti's unit of ground truth: nothing extracted from it is
    ever trusted more than the episode itself, and every fact keeps a link back
    to the episodes that support it.

    Attributes:
        block: Stored episode block.
        sequence: Position in the graph's ingestion event log.
        reference_time: When the source event happened.
        ingested_at: When this graph processed the episode.
    """

    block: MemoryBlock
    sequence: int
    reference_time: datetime
    ingested_at: datetime

    @property
    def id(self) -> str:
        """Return the episode block id."""

        return self.block.id

    @property
    def name(self) -> str:
        """Return the short episode name."""

        return required_string(self.block.value, "name")

    @property
    def content(self) -> str:
        """Return the raw episode body."""

        return required_string(self.block.value, "content")

    @property
    def source(self) -> str:
        """Return the episode source kind."""

        return required_string(self.block.value, "source")

    @property
    def source_description(self) -> str:
        """Return the free-text description of where the episode came from."""

        return required_string(self.block.value, "source_description")


@dataclass(frozen=True, slots=True)
class Entity:
    """One entity node together with its current summary revision.

    Attributes:
        block: Stored entity block, which also anchors its summary log.
        address: Keyed address that resolves this entity by type and name.
        summary: Text of the newest summary revision.
        revision: Logical revision number of that summary.
    """

    block: MemoryBlock
    address: MemoryAddress
    summary: str
    revision: int

    @property
    def id(self) -> str:
        """Return the entity block id."""

        return self.block.id

    @property
    def name(self) -> str:
        """Return the entity's display name."""

        return required_string(self.block.value, "name")

    @property
    def entity_type(self) -> str:
        """Return the application-defined entity type."""

        return required_string(self.block.value, "entity_type")


@dataclass(frozen=True, slots=True)
class SummaryRevision:
    """One immutable snapshot of an entity summary.

    Attributes:
        block: Stored summary block.
        revision: Logical revision number, starting at one.
        recorded_at: Ingestion time the revision was written for.
    """

    block: MemoryBlock
    revision: int
    recorded_at: datetime

    @property
    def text(self) -> str:
        """Return the summary text of this revision."""

        return required_string(self.block.value, "summary")


@dataclass(frozen=True, slots=True)
class TemporalFact:
    """A relationship fact placed on both time axes.

    Valid time answers "when was this true in the world"; transaction time
    answers "when did this graph hold it on record". Both intervals are
    half-open, and a missing end means still open.

    Attributes:
        block: Stored fact block holding the relation and its sentence.
        subject: Entity block at the source of the relationship.
        object: Entity block at the target of the relationship.
        valid: Valid-time interval, `[valid_at, invalid_at)`.
        recorded: Transaction-time interval, `[ingested_at, expired_at)`.
    """

    block: MemoryBlock
    subject: MemoryBlock
    object: MemoryBlock
    valid: ValidityInterval
    recorded: ValidityInterval

    @property
    def id(self) -> str:
        """Return the fact block id."""

        return self.block.id

    @property
    def relation(self) -> str:
        """Return the application-defined relationship name."""

        return required_string(self.block.value, "relation")

    @property
    def fact(self) -> str:
        """Return the readable sentence stored for this relationship."""

        return required_string(self.block.value, "fact")

    @property
    def statement(self) -> str:
        """Return the resolved triplet in `subject -[relation]-> object` form."""

        return f"{entity_name(self.subject)} -[{self.relation}]-> {entity_name(self.object)}"

    @property
    def valid_at(self) -> datetime:
        """Return when the fact became true in the world."""

        return self.valid.valid_at

    @property
    def invalid_at(self) -> datetime | None:
        """Return when the fact stopped being true, or `None` if it still is."""

        return self.valid.invalid_at

    @property
    def ingested_at(self) -> datetime:
        """Return when this graph first recorded the fact."""

        return self.recorded.valid_at

    @property
    def expired_at(self) -> datetime | None:
        """Return when this graph learned the fact was superseded."""

        return self.recorded.invalid_at

    def was_true_at(self, when: datetime) -> bool:
        """Report whether the fact was true in the world at an instant.

        Args:
            when: Timezone-aware effective time.

        Returns:
            `True` when `when` falls inside the valid-time interval.
        """

        return interval_contains(self.valid, normalize_timestamp(when, field="when"))

    def was_known_at(self, when: datetime) -> bool:
        """Report whether the graph held the fact on record at an instant.

        Args:
            when: Timezone-aware ingestion time.

        Returns:
            `True` when `when` falls inside the transaction-time interval.
        """

        return interval_contains(self.recorded, normalize_timestamp(when, field="when"))


@dataclass(frozen=True, slots=True)
class TripletResult:
    """Outcome of writing one subject-relation-object triplet.

    Attributes:
        fact: The fact now on record for the triplet.
        invalidated: Facts this write closed on both time axes.
        created: Whether a new fact block was stored. A repeated triplet
            merges provenance onto the existing fact instead.
    """

    fact: TemporalFact
    invalidated: tuple[TemporalFact, ...]
    created: bool


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def entity_address(name: str, *, entity_type: str) -> MemoryAddress:
    """Return the keyed address that identifies an entity.

    Names are normalized case-insensitively inside their type, which is this
    lamp's deterministic stand-in for embedding-based entity resolution.

    Args:
        name: Display name of the entity.
        entity_type: Application-defined entity type.

    Returns:
        The address that resolves this entity in the versioned record set.
    """

    return MemoryAddress((ENTITY_NAMESPACE, entity_type), name.strip().casefold())


def default_sentence(subject: Entity, relation: str, object: Entity) -> str:
    """Derive a readable fact sentence from a triplet.

    Args:
        subject: Subject entity.
        relation: Relation name.
        object: Object entity.

    Returns:
        A sentence stored on the fact block as `EntityEdge.fact` would be.
    """

    return f"{subject.name} {relation.replace('_', ' ')} {object.name}."


def interval_contains(interval: ValidityInterval, when: datetime) -> bool:
    """Report whether a half-open interval contains an instant.

    Args:
        interval: Half-open validity interval.
        when: Instant to test.

    Returns:
        `True` when `when` is at or after the start and before the end.
    """

    return interval.valid_at <= when and (interval.invalid_at is None or when < interval.invalid_at)


def entity_name(entity: MemoryBlock) -> str:
    """Return the display name stored on an entity block.

    Args:
        entity: Entity block.

    Returns:
        The stored name.
    """

    return required_string(entity.value, "name")


def required_string(mapping: Any, key: str) -> str:
    """Read a required string field from a dictionary-like block value.

    Args:
        mapping: Decoded block value.
        key: Field name.

    Returns:
        The stored string.

    Raises:
        TypeError: If the value is not a mapping or the field is not a string.
    """

    if not isinstance(mapping, dict) or not isinstance(mapping.get(key), str):
        raise TypeError(f"Expected string field {key!r}.")
    return mapping[key]


def required_metadata(block: MemoryBlock, key: str) -> str:
    """Read a required string field from block metadata.

    Args:
        block: Stored block.
        key: Metadata field name.

    Returns:
        The stored string.

    Raises:
        TypeError: If the metadata field is missing or not a string.
    """

    value = block.metadata.get(key)
    if not isinstance(value, str):
        raise TypeError(f"Block {block.id!r} is missing string metadata {key!r}.")
    return value


def require_text(value: object, *, field: str) -> None:
    """Validate that an application-supplied field holds non-empty text.

    Args:
        value: Supplied field value.
        field: Field name used in error messages.

    Raises:
        TypeError: If the value is not a string.
        ValueError: If the string is empty or only whitespace.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value.strip():
        raise ValueError(f"{field} cannot be empty.")
