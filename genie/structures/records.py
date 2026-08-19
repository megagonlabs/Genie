"""Immutable records shared by optional memory structures.

The records in this module describe where an existing memory block participates
in a structure. They contain block ids instead of block payloads, so one block
can be addressed, ordered, timed, traversed, and versioned without copying its
value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink


@dataclass(frozen=True, slots=True)
class MemoryAddress:
    """Stable key address inside a namespaced collection.

    Attributes:
        namespace: Ordered namespace segments. An empty tuple represents the
            collection root.
        key: Unique key within the namespace.
    """

    namespace: tuple[str, ...]
    key: str

    def __post_init__(self) -> None:
        """Validate and freeze the namespace and key."""

        if isinstance(self.namespace, str):
            raise TypeError("MemoryAddress namespace must be a tuple of strings.")
        try:
            namespace = tuple(self.namespace)
        except TypeError as exc:
            raise TypeError("MemoryAddress namespace must be a tuple of strings.") from exc
        for segment in namespace:
            _require_name(segment, field_name="namespace segment")
        object.__setattr__(self, "namespace", namespace)
        _require_name(self.key, field_name="key")


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """Bind one collection address to a memory block.

    Attributes:
        collection_id: Id of the collection that owns the key space.
        address: Namespace and key used to find the block.
        block_id: Id of the addressed memory block.
    """

    collection_id: str
    address: MemoryAddress
    block_id: str

    def __post_init__(self) -> None:
        """Validate binding identifiers and address type."""

        _require_id(self.collection_id, field_name="collection_id")
        if not isinstance(self.address, MemoryAddress):
            raise TypeError("KeyBinding address must be a MemoryAddress.")
        _require_id(self.block_id, field_name="block_id")


@dataclass(frozen=True, slots=True)
class KeyedItem:
    """A visible block together with its namespaced key binding.

    Attributes:
        binding: Structural key binding for the block.
        block: Visible memory block resolved through normal memory access.
    """

    binding: KeyBinding
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Validate that the binding and block refer to the same id."""

        if not isinstance(self.binding, KeyBinding):
            raise TypeError("KeyedItem binding must be a KeyBinding.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("KeyedItem block must be a MemoryBlock.")
        if self.binding.block_id != self.block.id:
            raise ValueError("KeyedItem binding and block ids must match.")


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Place a memory block at a stable position in an ordered log.

    Sequence numbers are non-negative and unique within one log. A later log
    facade and backend capability will define how positions are allocated
    atomically.

    Attributes:
        log_id: Id of the ordered log.
        sequence: Stable, non-negative position within the log.
        block_id: Id of the memory block at this position.
    """

    log_id: str
    sequence: int
    block_id: str

    def __post_init__(self) -> None:
        """Validate the log identifier, sequence, and block identifier."""

        _require_id(self.log_id, field_name="log_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("LogEntry sequence must be an integer.")
        if self.sequence < 0:
            raise ValueError("LogEntry sequence must be greater than or equal to zero.")
        _require_id(self.block_id, field_name="block_id")


@dataclass(frozen=True, slots=True)
class LogItem:
    """A visible block together with its ordered log entry.

    Attributes:
        entry: Structural position of the block in a log.
        block: Visible memory block resolved through normal memory access.
    """

    entry: LogEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Validate that the entry and block refer to the same id."""

        if not isinstance(self.entry, LogEntry):
            raise TypeError("LogItem entry must be a LogEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("LogItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("LogItem entry and block ids must match.")


@dataclass(frozen=True, slots=True)
class ValidityInterval:
    """Half-open period during which information is valid.

    The interval includes ``valid_at`` and excludes ``invalid_at``. A missing
    ``invalid_at`` means that the interval is open-ended. Timestamps must be
    timezone-aware and are normalized to UTC.

    Attributes:
        valid_at: Inclusive start of the validity period.
        invalid_at: Optional exclusive end of the validity period.
    """

    valid_at: datetime
    invalid_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate interval bounds and normalize them to UTC."""

        valid_at = _utc_datetime(self.valid_at, field_name="valid_at")
        invalid_at = (
            _utc_datetime(self.invalid_at, field_name="invalid_at")
            if self.invalid_at is not None
            else None
        )
        if invalid_at is not None and invalid_at <= valid_at:
            raise ValueError("ValidityInterval invalid_at must be later than valid_at.")
        object.__setattr__(self, "valid_at", valid_at)
        object.__setattr__(self, "invalid_at", invalid_at)


@dataclass(frozen=True, slots=True)
class TemporalEntry:
    """Attach event time or a validity period to a memory block.

    At least one of ``occurred_at`` or ``validity`` must be present. Event time
    and validity are separate because an event may happen once while a fact
    remains valid for a longer period.

    Attributes:
        collection_id: Id of the temporal collection.
        block_id: Id of the timed memory block.
        occurred_at: Optional time when an event happened.
        validity: Optional period during which information is valid.
    """

    collection_id: str
    block_id: str
    occurred_at: datetime | None = None
    validity: ValidityInterval | None = None

    def __post_init__(self) -> None:
        """Validate identifiers, time fields, and temporal membership."""

        _require_id(self.collection_id, field_name="collection_id")
        _require_id(self.block_id, field_name="block_id")
        if self.occurred_at is None and self.validity is None:
            raise ValueError("TemporalEntry requires occurred_at, validity, or both.")
        if self.occurred_at is not None:
            object.__setattr__(
                self,
                "occurred_at",
                _utc_datetime(self.occurred_at, field_name="occurred_at"),
            )
        if self.validity is not None and not isinstance(self.validity, ValidityInterval):
            raise TypeError("TemporalEntry validity must be a ValidityInterval.")


@dataclass(frozen=True, slots=True)
class TemporalItem:
    """A visible block together with its temporal membership.

    Attributes:
        entry: Event time or validity period attached to the block.
        block: Visible memory block resolved through normal memory access.
    """

    entry: TemporalEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Validate that the entry and block refer to the same id."""

        if not isinstance(self.entry, TemporalEntry):
            raise TypeError("TemporalItem entry must be a TemporalEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("TemporalItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("TemporalItem entry and block ids must match.")


@dataclass(frozen=True, slots=True)
class GraphPath:
    """An ordered path through memory blocks and typed links.

    A path contains one more block id than link. Each link must connect its two
    neighboring block ids in either direction. A one-block path with no links
    is valid.

    Attributes:
        block_ids: Block ids in traversal order.
        links: Links followed between neighboring blocks.
    """

    block_ids: tuple[str, ...]
    links: tuple[MemoryLink, ...] = ()

    def __post_init__(self) -> None:
        """Freeze and validate the path contents and continuity."""

        if isinstance(self.block_ids, str):
            raise TypeError("GraphPath block_ids must be a tuple of strings.")
        try:
            block_ids = tuple(self.block_ids)
            links = tuple(self.links)
        except TypeError as exc:
            raise TypeError("GraphPath block_ids and links must be tuples.") from exc
        if not block_ids:
            raise ValueError("GraphPath requires at least one block id.")
        for block_id in block_ids:
            _require_id(block_id, field_name="block_id")
        if len(block_ids) != len(links) + 1:
            raise ValueError("GraphPath must contain one more block id than link.")
        for index, link in enumerate(links):
            if not isinstance(link, MemoryLink):
                raise TypeError("GraphPath links must be MemoryLink instances.")
            left = block_ids[index]
            right = block_ids[index + 1]
            if not (
                (link.source_id == left and link.target_id == right)
                or (link.source_id == right and link.target_id == left)
            ):
                raise ValueError(
                    f"GraphPath link {link.id!r} does not connect neighboring blocks "
                    f"{left!r} and {right!r}."
                )
        object.__setattr__(self, "block_ids", block_ids)
        object.__setattr__(self, "links", links)

    @property
    def start_id(self) -> str:
        """Return the first block id in the path."""

        return self.block_ids[0]

    @property
    def end_id(self) -> str:
        """Return the last block id in the path."""

        return self.block_ids[-1]

    def __len__(self) -> int:
        """Return the number of links in the path."""

        return len(self.links)


@dataclass(frozen=True, slots=True)
class GraphNeighbor:
    """A neighboring block reached through one typed link.

    ``direction`` is relative to the block from which the caller requested
    neighbors. An outgoing step reaches the link target; an incoming step
    reaches the link source. Parallel links produce separate neighbor records.

    Attributes:
        block: Visible neighboring memory block.
        link: Link used to reach the neighboring block.
        direction: Link direction relative to the starting block.
    """

    block: MemoryBlock
    link: MemoryLink
    direction: Literal["out", "in"]

    def __post_init__(self) -> None:
        """Validate the neighbor block, link, and relative direction."""

        if not isinstance(self.block, MemoryBlock):
            raise TypeError("GraphNeighbor block must be a MemoryBlock.")
        if not isinstance(self.link, MemoryLink):
            raise TypeError("GraphNeighbor link must be a MemoryLink.")
        if self.direction not in {"out", "in"}:
            raise ValueError("GraphNeighbor direction must be 'out' or 'in'.")
        expected_id = (
            self.link.target_id if self.direction == "out" else self.link.source_id
        )
        if self.block.id != expected_id:
            raise ValueError(
                f"GraphNeighbor block {self.block.id!r} is not the "
                f"{self.direction} neighbor for link {self.link.id!r}."
            )


@dataclass(frozen=True, slots=True)
class RevisionEntry:
    """Place an immutable block revision under a stable logical record id.

    ``revision`` is separate from ``MemoryBlock.version``. A block version is
    the update counter for one stored block, while a revision identifies one
    preserved block in a logical record's history.

    Attributes:
        record_id: Stable logical identity shared by all revisions.
        revision: Positive revision number.
        block_id: Id of the memory block holding this revision's value.
    """

    record_id: str
    revision: int
    block_id: str

    def __post_init__(self) -> None:
        """Validate the record identifier, revision, and block identifier."""

        _require_id(self.record_id, field_name="record_id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("RevisionEntry revision must be an integer.")
        if self.revision < 1:
            raise ValueError("RevisionEntry revision must be greater than or equal to one.")
        _require_id(self.block_id, field_name="block_id")


@dataclass(frozen=True, slots=True)
class RevisionItem:
    """A visible block together with its logical revision entry.

    Attributes:
        entry: Stable record id and positive logical revision number.
        block: Visible memory block holding the revision value.
    """

    entry: RevisionEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Validate that the entry and block refer to the same id."""

        if not isinstance(self.entry, RevisionEntry):
            raise TypeError("RevisionItem entry must be a RevisionEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("RevisionItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("RevisionItem entry and block ids must match.")


def _require_name(value: object, *, field_name: str) -> None:
    """Require a non-empty string used as a structural name."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")


def _require_id(value: object, *, field_name: str) -> None:
    """Require a non-empty string used as a structural identifier."""

    _require_name(value, field_name=field_name)


def _utc_datetime(value: object, *, field_name: str) -> datetime:
    """Require a timezone-aware datetime and normalize it to UTC."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)
