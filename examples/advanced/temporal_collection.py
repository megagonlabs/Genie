"""Use one temporal collection for event time and validity queries.

Run from the repository root:

    python examples/advanced/temporal_collection.py
"""

from __future__ import annotations

from datetime import UTC, datetime

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory
from genie.structures import TemporalCollection, ValidityInterval


def at(day: int) -> datetime:
    """Return a fixed UTC time in January 2026."""

    return datetime(2026, 1, day, tzinfo=UTC)


def main() -> None:
    """Store events and facts in one temporal collection."""

    print("== Temporal collection ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    timeline_root = memory.put(type="timeline", value="deployment memory")
    deployed = memory.put(type="event", value="version 2 deployed")
    current_region = memory.put(type="fact", value="primary region is us-west")
    old_region = memory.put(type="fact", value="primary region was us-east")
    timeline = TemporalCollection(memory, backend, collection=timeline_root)

    timeline.add(deployed, occurred_at=at(10))
    timeline.add(
        current_region,
        validity=ValidityInterval(valid_at=at(10)),
    )
    timeline.add(
        old_region,
        validity=ValidityInterval(valid_at=at(1), invalid_at=at(10)),
    )

    deployment_events = timeline.events(start=at(1), stop=at(20))
    facts_on_day_five = timeline.valid_at(at(5))
    facts_on_day_fifteen = timeline.valid_at(at(15))
    overlapping_transition = timeline.overlapping(ValidityInterval(at(9), at(11)))

    print(f"events before Jan 20, 2026: {[item.block.value for item in deployment_events]}")
    print(f"facts valid on Jan 5, 2026: {[item.block.value for item in facts_on_day_five]}")
    print(f"facts valid on Jan 15, 2026: {[item.block.value for item in facts_on_day_fifteen]}")
    print(f"facts overlapping the transition: {len(overlapping_transition)}")

    assert [item.block for item in deployment_events] == [deployed]
    assert [item.block for item in facts_on_day_five] == [old_region]
    assert [item.block for item in facts_on_day_fifteen] == [current_region]
    assert {item.block.id for item in overlapping_transition} == {
        old_region.id,
        current_region.id,
    }

    # scan applies every supplied filter at once. An entry missing a time field
    # a filter needs does not match, so combining bounds narrows the result.
    rollout = memory.put(type="fact", value="rollout window is open")
    timeline.add(
        rollout,
        occurred_at=at(12),
        validity=ValidityInterval(valid_at=at(12), invalid_at=at(20)),
    )

    dated = timeline.events(start=at(1), stop=at(20))
    valid_mid_january = timeline.valid_at(at(15))
    both = timeline.scan(occurred_start=at(1), occurred_stop=at(20), valid_at=at(15))

    print(f"entries with an event time in range: {len(dated)}")
    print(f"entries valid on Jan 15, 2026: {len(valid_mid_january)}")
    print(f"entries matching both bounds: {[item.block.value for item in both]}")

    assert {item.block.id for item in dated} == {deployed.id, rollout.id}
    assert {item.block.id for item in valid_mid_january} == {current_region.id, rollout.id}
    # The deployment has no validity and the region fact has no event time, so
    # only the rollout window satisfies both filters.
    assert [item.block.id for item in both] == [rollout.id]

    timeline.replace(
        old_region,
        validity=ValidityInterval(valid_at=at(1), invalid_at=at(8)),
    )
    assert timeline.valid_at(at(9)) == []

    # Removing structure membership leaves the referenced block intact.
    timeline.remove(deployed)
    assert timeline.get(deployed) is None
    assert memory.get(deployed) == deployed
    print("replace and remove changed temporal membership, not block values")


if __name__ == "__main__":
    main()
