"""Immutable records shared by optional memory structures.

- **Membership by id:** the records in this module tell where an existing
  memory block is in a structure.
- **No payload copies:** they hold block ids, not block payloads. So one block
  can have an address, an order, a time, graph paths, and versions, with no
  copy of its value.
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
        namespace: Ordered namespace segments. An empty tuple is the
            collection root. The address stores the segments as a tuple.
        key: Unique key in the namespace.

    Raises:
        TypeError: If `namespace` is a `str` or is not iterable, a namespace
            segment is not a string, or `key` is not a string.
        ValueError: If a namespace segment or `key` is empty or contains only
            whitespace.
    """

    namespace: tuple[str, ...]
    key: str

    def __post_init__(self) -> None:
        """Check the namespace and the key, and freeze the namespace as a tuple."""

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
        address: Namespace and key that find the block.
        block_id: Id of the memory block at the address.

    Raises:
        TypeError: If an id is not a string or `address` is not a
            `MemoryAddress`.
        ValueError: If an id is empty or contains only whitespace.
    """

    collection_id: str
    address: MemoryAddress
    block_id: str

    def __post_init__(self) -> None:
        """Check the binding ids and the address type."""

        _require_id(self.collection_id, field_name="collection_id")
        if not isinstance(self.address, MemoryAddress):
            raise TypeError("KeyBinding address must be a MemoryAddress.")
        _require_id(self.block_id, field_name="block_id")


@dataclass(frozen=True, slots=True)
class KeyedItem:
    """A visible block together with its namespaced key binding.

    Attributes:
        binding: Structural key binding for the block.
        block: Visible memory block that normal memory access resolved.

    Raises:
        TypeError: If `binding` is not a `KeyBinding` or `block` is not a
            `MemoryBlock`.
        ValueError: If the binding and block refer to different ids.
    """

    binding: KeyBinding
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Check that the binding and the block refer to the same id."""

        if not isinstance(self.binding, KeyBinding):
            raise TypeError("KeyedItem binding must be a KeyBinding.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("KeyedItem block must be a MemoryBlock.")
        if self.binding.block_id != self.block.id:
            raise ValueError("KeyedItem binding and block ids must match.")


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Place a memory block at a stable position in an ordered log.

    - **Sequence numbers:** they are non-negative and unique in one log.
    - **Allocation:** the `SequenceStore` capability allocates positions
      atomically, and `EventLog` uses it.

    Attributes:
        log_id: Id of the ordered log.
        sequence: Stable, non-negative position in the log.
        block_id: Id of the memory block at this position.

    Raises:
        TypeError: If an id is not a string or `sequence` is not an integer.
        ValueError: If an id is empty or contains only whitespace, or
            `sequence` is negative.
    """

    log_id: str
    sequence: int
    block_id: str

    def __post_init__(self) -> None:
        """Check the log id, the sequence, and the block id."""

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
        block: Visible memory block that normal memory access resolved.

    Raises:
        TypeError: If `entry` is not a `LogEntry` or `block` is not a
            `MemoryBlock`.
        ValueError: If the entry and block refer to different ids.
    """

    entry: LogEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Check that the entry and the block refer to the same id."""

        if not isinstance(self.entry, LogEntry):
            raise TypeError("LogItem entry must be a LogEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("LogItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("LogItem entry and block ids must match.")


@dataclass(frozen=True, slots=True)
class ValidityInterval:
    """Half-open period in which information is valid.

    - **Half-open:** the interval includes `valid_at` and excludes `invalid_at`.
    - **Open end:** with no `invalid_at`, the interval has no end.
    - **Time zones:** both timestamps are timezone-aware. The interval stores
      them in UTC.

    Attributes:
        valid_at: Inclusive start of the validity period.
        invalid_at: Optional exclusive end of the validity period.

    Raises:
        TypeError: If a bound is not a datetime.
        ValueError: If a bound is naive or `invalid_at` is not later than
            `valid_at`.
    """

    valid_at: datetime
    invalid_at: datetime | None = None

    def __post_init__(self) -> None:
        """Check the interval bounds, and normalize them to UTC."""

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

    - **At least one time:** an entry has `occurred_at`, `validity`, or both.
    - **Two separate times:** an event can happen once, but a fact can stay
      valid for a longer period. So event time and validity are separate.

    Attributes:
        collection_id: Id of the temporal collection.
        block_id: Id of the timed memory block.
        occurred_at: Optional time when an event happened. The entry stores it
            in UTC.
        validity: Optional period in which the information is valid.

    Raises:
        TypeError: If an id is not a string, `occurred_at` is not a datetime,
            or `validity` is not a `ValidityInterval`.
        ValueError: If an id is empty or contains only whitespace,
            `occurred_at` is naive, or neither `occurred_at` nor `validity` is
            given.
    """

    collection_id: str
    block_id: str
    occurred_at: datetime | None = None
    validity: ValidityInterval | None = None

    def __post_init__(self) -> None:
        """Check the ids, the time fields, and that the entry has at least one time."""

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
        block: Visible memory block that normal memory access resolved.

    Raises:
        TypeError: If `entry` is not a `TemporalEntry` or `block` is not a
            `MemoryBlock`.
        ValueError: If the entry and block refer to different ids.
    """

    entry: TemporalEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Check that the entry and the block refer to the same id."""

        if not isinstance(self.entry, TemporalEntry):
            raise TypeError("TemporalItem entry must be a TemporalEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("TemporalItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("TemporalItem entry and block ids must match.")


@dataclass(frozen=True, slots=True)
class GraphPath:
    """An ordered path through memory blocks and typed links.

    - **Counts:** a path has one more block id than links.
    - **Continuity:** each link connects its 2 adjacent block ids, in either
      direction.
    - **Zero links:** a path with one block and no links is valid.

    Attributes:
        block_ids: Block ids in traversal order.
        links: Links that the path follows between adjacent blocks.

    Raises:
        TypeError: If `block_ids` is a `str`, `block_ids` or `links` is not
            iterable, a block id is not a string, or a link is not a
            `MemoryLink`.
        ValueError: If `block_ids` is empty, a block id is empty, the counts
            do not differ by exactly one, or a link does not connect its
            adjacent blocks.
    """

    block_ids: tuple[str, ...]
    links: tuple[MemoryLink, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the path contents as tuples, and check them and the continuity."""

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
    """A neighbor block that one typed link reaches.

    - **Direction:** `direction` is relative to the block whose neighbors the
      caller requested. An outgoing step gets to the link target. An incoming
      step gets to the link source.
    - **Parallel links:** each parallel link gives a separate neighbor record.

    Attributes:
        block: Visible neighbor memory block.
        link: Link that reaches the neighbor block.
        direction: Link direction relative to the starting block.

    Raises:
        TypeError: If `block` is not a `MemoryBlock` or `link` is not a
            `MemoryLink`.
        ValueError: If `direction` is not `"out"` or `"in"`, or `block` is not
            the neighbor that `direction` selects.
    """

    block: MemoryBlock
    link: MemoryLink
    direction: Literal["out", "in"]

    def __post_init__(self) -> None:
        """Check the neighbor block, the link, and the relative direction."""

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

    - **Two counters:** `revision` is separate from `MemoryBlock.version`. A
      block version counts the updates of one stored block. A revision
      identifies one kept block in the history of a logical record.
    - **Numbers:** `VersionedRecords` sets `revision` to the log sequence plus
      one. So the first revision is 1, and a deleted revision block leaves a
      gap.

    Attributes:
        record_id: Stable logical identity that all revisions share.
        revision: Positive revision number.
        block_id: Id of the memory block that holds the value of this revision.

    Raises:
        TypeError: If an id is not a string or `revision` is not an integer.
        ValueError: If an id is empty or contains only whitespace, or
            `revision` is less than one.
    """

    record_id: str
    revision: int
    block_id: str

    def __post_init__(self) -> None:
        """Check the record id, the revision, and the block id."""

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
        block: Visible memory block that holds the revision value.

    Raises:
        TypeError: If `entry` is not a `RevisionEntry` or `block` is not a
            `MemoryBlock`.
        ValueError: If the entry and block refer to different ids.
    """

    entry: RevisionEntry
    block: MemoryBlock

    def __post_init__(self) -> None:
        """Check that the entry and the block refer to the same id."""

        if not isinstance(self.entry, RevisionEntry):
            raise TypeError("RevisionItem entry must be a RevisionEntry.")
        if not isinstance(self.block, MemoryBlock):
            raise TypeError("RevisionItem block must be a MemoryBlock.")
        if self.entry.block_id != self.block.id:
            raise ValueError("RevisionItem entry and block ids must match.")


def _require_name(value: object, *, field_name: str) -> None:
    """Raise an error if a structural name is not a string, or is empty or only whitespace."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")


def _require_id(value: object, *, field_name: str) -> None:
    """Raise an error if a structural id is not a string, or is empty or only whitespace."""

    _require_name(value, field_name=field_name)


def _utc_datetime(value: object, *, field_name: str) -> datetime:
    """Check that a value is a timezone-aware datetime, and return it in UTC."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)
