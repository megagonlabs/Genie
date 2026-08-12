"""Tests for immutable structural records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from genie import MemoryBlock, MemoryLink
from genie.structures import (
    GraphNeighbor,
    GraphPath,
    KeyBinding,
    KeyedItem,
    LogEntry,
    LogItem,
    MemoryAddress,
    RevisionEntry,
    RevisionItem,
    TemporalEntry,
    TemporalItem,
    ValidityInterval,
)


def test_key_binding_uses_a_stable_namespaced_address() -> None:
    """Key bindings reference a block without copying its value."""

    address = MemoryAddress(namespace=("users", "alice"), key="language")
    binding = KeyBinding(
        collection_id="preferences",
        address=address,
        block_id="mem_language",
    )

    assert binding.address.namespace == ("users", "alice")
    assert binding.address.key == "language"
    assert binding.block_id == "mem_language"
    with pytest.raises(FrozenInstanceError):
        binding.block_id = "mem_other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("namespace", "key", "error"),
    [
        (("users", ""), "language", ValueError),
        (("users",), "", ValueError),
        ("users", "language", TypeError),
        (("users", 1), "language", TypeError),
    ],
)
def test_memory_address_rejects_invalid_names(
    namespace: object,
    key: object,
    error: type[Exception],
) -> None:
    """Namespace segments and keys must be non-empty strings."""

    with pytest.raises(error):
        MemoryAddress(namespace=namespace, key=key)  # type: ignore[arg-type]


def test_log_entry_accepts_non_negative_integer_sequences() -> None:
    """A log position is stable, integral, and may start at zero."""

    assert LogEntry(log_id="conversation", sequence=0, block_id="mem_event").sequence == 0

    with pytest.raises(ValueError):
        LogEntry(log_id="conversation", sequence=-1, block_id="mem_event")
    with pytest.raises(TypeError):
        LogEntry(log_id="conversation", sequence=True, block_id="mem_event")


def test_resolved_keyed_and_log_items_require_matching_block_ids() -> None:
    """Resolved structure items cannot pair a placement with another block."""

    block = MemoryBlock(type="event", value="event", id="mem_event")
    binding = KeyBinding(
        collection_id="collection",
        address=MemoryAddress((), "key"),
        block_id=block.id,
    )
    entry = LogEntry(log_id="log", sequence=0, block_id=block.id)

    assert KeyedItem(binding, block).block == block
    assert LogItem(entry, block).block == block
    other = MemoryBlock(type="event", value="other", id="mem_other")
    with pytest.raises(ValueError, match="ids must match"):
        KeyedItem(binding, other)
    with pytest.raises(ValueError, match="ids must match"):
        LogItem(entry, other)


def test_validity_interval_normalizes_aware_times_to_utc() -> None:
    """Validity periods use aware UTC times and half-open ordered bounds."""

    pacific = timezone(timedelta(hours=-8))
    interval = ValidityInterval(
        valid_at=datetime(2026, 1, 1, 8, tzinfo=pacific),
        invalid_at=datetime(2026, 2, 1, 8, tzinfo=pacific),
    )

    assert interval.valid_at == datetime(2026, 1, 1, 16, tzinfo=UTC)
    assert interval.invalid_at == datetime(2026, 2, 1, 16, tzinfo=UTC)


def test_validity_interval_rejects_naive_or_reversed_bounds() -> None:
    """Invalid temporal boundaries fail when the record is created."""

    with pytest.raises(ValueError, match="timezone-aware"):
        ValidityInterval(valid_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="later than"):
        ValidityInterval(
            valid_at=datetime(2026, 2, 1, tzinfo=UTC),
            invalid_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="later than"):
        ValidityInterval(
            valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            invalid_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_temporal_entry_supports_event_time_validity_or_both() -> None:
    """Point events and validity periods remain separate temporal concepts."""

    occurred_at = datetime(2026, 1, 15, tzinfo=UTC)
    validity = ValidityInterval(valid_at=datetime(2026, 1, 1, tzinfo=UTC))

    point = TemporalEntry("timeline", "mem_event", occurred_at=occurred_at)
    period = TemporalEntry("facts", "mem_fact", validity=validity)
    both = TemporalEntry(
        "facts",
        "mem_observed_fact",
        occurred_at=occurred_at,
        validity=validity,
    )

    assert point.occurred_at == occurred_at
    assert period.validity == validity
    assert both.occurred_at == occurred_at
    assert both.validity == validity
    with pytest.raises(ValueError, match="requires"):
        TemporalEntry("timeline", "mem_event")


def test_temporal_item_requires_matching_block_id() -> None:
    """Resolved temporal membership cannot be paired with another block."""

    entry = TemporalEntry(
        "timeline",
        "mem_event",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    block = MemoryBlock(type="event", value="event", id="mem_event")

    assert TemporalItem(entry, block).block == block
    with pytest.raises(ValueError, match="ids must match"):
        TemporalItem(
            entry,
            MemoryBlock(type="event", value="other", id="mem_other"),
        )


def test_graph_path_validates_forward_and_reverse_traversal() -> None:
    """A graph path may follow directed links in either traversal direction."""

    first = MemoryLink(
        id="link_first",
        source_id="mem_a",
        target_id="mem_b",
        relation="related",
    )
    second = MemoryLink(
        id="link_second",
        source_id="mem_c",
        target_id="mem_b",
        relation="related",
    )
    path = GraphPath(
        block_ids=("mem_a", "mem_b", "mem_c"),
        links=(first, second),
    )

    assert path.start_id == "mem_a"
    assert path.end_id == "mem_c"
    assert len(path) == 2


def test_graph_path_rejects_broken_paths() -> None:
    """Every path link must connect its neighboring block ids."""

    link = MemoryLink(
        id="link_unrelated",
        source_id="mem_x",
        target_id="mem_y",
        relation="related",
    )

    with pytest.raises(ValueError, match="one more block id"):
        GraphPath(block_ids=("mem_a",), links=(link,))
    with pytest.raises(ValueError, match="does not connect"):
        GraphPath(block_ids=("mem_a", "mem_b"), links=(link,))
    with pytest.raises(ValueError, match="at least one"):
        GraphPath(block_ids=())


def test_graph_neighbor_validates_direction_and_endpoint() -> None:
    """A resolved neighbor must match one endpoint and a valid direction."""

    source = MemoryBlock(type="node", value="source", id="mem_source")
    target = MemoryBlock(type="node", value="target", id="mem_target")
    link = MemoryLink(source.id, target.id, "related")

    assert GraphNeighbor(target, link, "out").block == target
    assert GraphNeighbor(source, link, "in").block == source
    with pytest.raises(ValueError, match="GraphNeighbor direction"):
        GraphNeighbor(source, link, "sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not the out neighbor"):
        GraphNeighbor(source, link, "out")


@pytest.mark.parametrize(
    "record",
    [
        lambda: KeyBinding("collection", cast(MemoryAddress, "bad"), "mem_block"),
        lambda: KeyedItem(cast(KeyBinding, "bad"), MemoryBlock("note", "value")),
        lambda: LogItem(cast(LogEntry, "bad"), MemoryBlock("note", "value")),
        lambda: TemporalEntry(
            "timeline",
            "mem_block",
            validity=cast(ValidityInterval, "bad"),
        ),
        lambda: TemporalItem(
            cast(TemporalEntry, "bad"),
            MemoryBlock("note", "value"),
        ),
        lambda: RevisionItem(
            cast(RevisionEntry, "bad"),
            MemoryBlock("note", "value"),
        ),
    ],
)
def test_structural_records_reject_wrong_component_types(
    record: Callable[[], object],
) -> None:
    """Resolved records fail early when nested components have the wrong type."""

    with pytest.raises(TypeError):
        record()


def test_revision_entry_has_a_separate_positive_revision_number() -> None:
    """Logical revisions are positive and independent of block update counters."""

    entry = RevisionEntry(record_id="favorite_color", revision=1, block_id="mem_blue")

    assert entry.revision == 1
    with pytest.raises(ValueError):
        RevisionEntry(record_id="favorite_color", revision=0, block_id="mem_blue")
    with pytest.raises(TypeError):
        RevisionEntry(record_id="favorite_color", revision=False, block_id="mem_blue")


def test_revision_item_requires_matching_block_id() -> None:
    """Resolved revision membership cannot be paired with another block."""

    entry = RevisionEntry(record_id="favorite_color", revision=1, block_id="mem_blue")
    block = MemoryBlock(type="revision", value="blue", id="mem_blue")

    assert RevisionItem(entry, block).block == block
    with pytest.raises(ValueError, match="ids must match"):
        RevisionItem(
            entry,
            MemoryBlock(type="revision", value="green", id="mem_green"),
        )


@pytest.mark.parametrize(
    "record",
    [
        lambda: KeyBinding("", MemoryAddress((), "key"), "mem_block"),
        lambda: LogEntry("log", 1, ""),
        lambda: TemporalEntry(
            "",
            "mem_block",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        lambda: RevisionEntry("record", 1, ""),
    ],
)
def test_structural_records_reject_empty_identifiers(
    record: Callable[[], object],
) -> None:
    """Every structural reference uses a non-empty string identifier."""

    with pytest.raises(ValueError):
        record()
