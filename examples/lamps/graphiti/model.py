"""Value types, constants, and time helpers for the Graphiti lamp.

- **Upstream equivalent:** Graphiti keeps its node and edge models in separate
  modules. This module does the same job for the lamp.
- **Names:** the block types and relation names that the lamp writes.
- **Time helpers:** the timestamp helpers that all layers of the lamp share.
- **Records:** the read-only records that the graph returns. These are an
  ingested episode, an entity with its current summary, one summary revision, a
  fact on both time axes, and the result of a triplet write.
- **No import cycle:** the module uses Genie only to read blocks that are
  already stored. So every other module in the lamp can import it without a
  cycle.
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
    """Return a timezone-aware timestamp, converted to UTC.

    Args:
        value: Timestamp from the application code.
        field: Field name for the error messages.

    Returns:
        The same instant in UTC.

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
        An ISO 8601 string that ends in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name for the error messages.

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

    - **Ground truth:** in Graphiti, an episode is the unit of ground truth.
      Nothing that the graph extracts from an episode gets more trust than the
      episode.
    - **Provenance:** every fact keeps a link to the episodes that support it.

    Attributes:
        block: Stored episode block.
        sequence: Position of the episode in the ingestion event log of the
            graph.
        reference_time: Time of the source event.
        ingested_at: Time when this graph processed the episode.
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
        """Return the free-text description of the episode source."""

        return required_string(self.block.value, "source_description")


@dataclass(frozen=True, slots=True)
class Entity:
    """One entity node and its current summary revision.

    Attributes:
        block: Stored entity block. It is also the anchor of the summary log of
            the entity.
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
        """Return the display name of the entity."""

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
        revision: Logical revision number. The first revision is 1.
        recorded_at: Ingestion time for which the graph wrote the revision.
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
    """A relationship fact on both time axes.

    - **Valid time:** tells when the fact was true in the world.
    - **Transaction time:** tells when this graph held the fact on record.
    - **Half-open intervals:** each interval includes its start and does not
      include its end. An interval with no end is still open.

    Attributes:
        block: Stored fact block that holds the relation and its sentence.
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
        """Return the readable sentence that the block stores for the fact."""

        return required_string(self.block.value, "fact")

    @property
    def statement(self) -> str:
        """Return the resolved triplet in `subject -[relation]-> object` form."""

        return f"{entity_name(self.subject)} -[{self.relation}]-> {entity_name(self.object)}"

    @property
    def valid_at(self) -> datetime:
        """Return the time when the fact became true in the world."""

        return self.valid.valid_at

    @property
    def invalid_at(self) -> datetime | None:
        """Return the end of valid time, or `None` if the fact is still true."""

        return self.valid.invalid_at

    @property
    def ingested_at(self) -> datetime:
        """Return the time when this graph first recorded the fact."""

        return self.recorded.valid_at

    @property
    def expired_at(self) -> datetime | None:
        """Return the end of transaction time, or `None` if it is still open.

        The end is the time when this graph learned that a newer fact
        superseded this fact.
        """

        return self.recorded.invalid_at

    def was_true_at(self, when: datetime) -> bool:
        """Report if the fact was true in the world at an instant.

        Args:
            when: Timezone-aware effective time.

        Returns:
            `True` if `when` is inside the valid-time interval.
        """

        return interval_contains(self.valid, normalize_timestamp(when, field="when"))

    def was_known_at(self, when: datetime) -> bool:
        """Report if the graph held the fact on record at an instant.

        Args:
            when: Timezone-aware ingestion time.

        Returns:
            `True` if `when` is inside the transaction-time interval.
        """

        return interval_contains(self.recorded, normalize_timestamp(when, field="when"))


@dataclass(frozen=True, slots=True)
class TripletResult:
    """Result of one write of a subject-relation-object triplet.

    Attributes:
        fact: The fact that is now on record for the triplet.
        invalidated: Facts that this write closed on both time axes.
        created: `True` if the write stored a new fact block. For a repeated
            triplet, the write adds provenance to the existing fact and does not
            store a new block.
    """

    fact: TemporalFact
    invalidated: tuple[TemporalFact, ...]
    created: bool


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def entity_address(name: str, *, entity_type: str) -> MemoryAddress:
    """Return the keyed address that identifies an entity.

    - **Rule:** the address uses the entity type and the name. It case-folds the
      name and removes the whitespace at the start and end, so names match
      without case sensitivity inside their type.
    - **Upstream difference:** this rule is the deterministic replacement in
      this lamp for entity resolution with embeddings.

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
        The sentence for the fact block, in the same role as `EntityEdge.fact`
        in Graphiti.
    """

    return f"{subject.name} {relation.replace('_', ' ')} {object.name}."


def interval_contains(interval: ValidityInterval, when: datetime) -> bool:
    """Report if a half-open interval contains an instant.

    Args:
        interval: Half-open validity interval.
        when: Instant to test.

    Returns:
        `True` if `when` is at or after the start and before the end. An
        interval with no end contains every instant after its start.
    """

    return interval.valid_at <= when and (interval.invalid_at is None or when < interval.invalid_at)


def entity_name(entity: MemoryBlock) -> str:
    """Return the display name that an entity block stores.

    Args:
        entity: Entity block.

    Returns:
        The stored name.
    """

    return required_string(entity.value, "name")


def required_string(mapping: Any, key: str) -> str:
    """Read a required string field from a dictionary block value.

    Args:
        mapping: Decoded block value.
        key: Field name.

    Returns:
        The stored string.

    Raises:
        TypeError: If the value is not a `dict` or the field is not a string.
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
        TypeError: If the metadata field does not exist or is not a string.
    """

    value = block.metadata.get(key)
    if not isinstance(value, str):
        raise TypeError(f"Block {block.id!r} is missing string metadata {key!r}.")
    return value


def require_text(value: object, *, field: str) -> None:
    """Validate that a field from the application holds text that is not empty.

    Args:
        value: Field value from the application.
        field: Field name for the error messages.

    Raises:
        TypeError: If the value is not a string.
        ValueError: If the string is empty or contains only whitespace.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    if not value.strip():
        raise ValueError(f"{field} cannot be empty.")
