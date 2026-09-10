"""Compose all seven optional structures around the same memory blocks.

Run from the repository root:

    python examples/advanced/experiment_memory.py
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

from genie import InMemoryBackend, Memory, MemoryConflict
from genie.structures import (
    EventLog,
    KeyedCollection,
    LineageDAG,
    MemoryAddress,
    OwnershipTree,
    PropertyGraph,
    TemporalCollection,
    ValidityInterval,
    VersionedRecords,
)


def main() -> None:
    """Use the characteristic operations of every optional structure."""

    print("== Experiment memory using all seven structures ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    workspace = memory.put(type="workspace", value="retrieval experiments")
    handle = memory.get_handle(root=workspace, allow_up=True)
    tree = OwnershipTree(handle, root=workspace)

    catalog_root = tree.create_child(type="collection", value="experiment catalog")
    events_root = tree.create_child(type="log", value="experiment events")
    timeline_root = tree.create_child(type="timeline", value="experiment timeline")
    versions_root = tree.create_child(type="versions", value="experiment versions")
    experiment = tree.create_child(
        type="experiment_identity",
        value="candidate-set experiment",
    )
    first_result = tree.create_child(
        parent=experiment,
        type="experiment_revision",
        value={"candidate_count": 20, "recall": 0.78},
    )
    second_result = tree.create_child(
        parent=experiment,
        type="experiment_revision",
        value={"candidate_count": 40, "recall": 0.84},
    )

    graph = PropertyGraph(handle)
    catalog = KeyedCollection(handle, backend, collection=catalog_root)
    events = EventLog(handle, backend, log=events_root)
    timeline = TemporalCollection(handle, backend, collection=timeline_root)
    lineage = LineageDAG(handle, relation="derived_into")
    versions = VersionedRecords(handle, backend, collection=versions_root)
    catalog_address = MemoryAddress(("retrieval",), "current_experiment")
    record_address = MemoryAddress(("retrieval",), "candidate_set")

    produced = graph.add_edge(experiment, first_result, "produced")
    catalog.bind(catalog_address, experiment)
    catalog.bind(MemoryAddress(("retrieval", "runs"), "baseline"), first_result)
    catalog.bind(MemoryAddress(("retrieval", "runs"), "tuned"), second_result)
    events.append(experiment)
    events.append(first_result)
    events.append(second_result)
    timeline.add(
        experiment,
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    timeline.add(
        first_result,
        occurred_at=datetime(2026, 1, 11, tzinfo=UTC),
        validity=ValidityInterval(
            datetime(2026, 1, 11, tzinfo=UTC),
            datetime(2026, 1, 12, tzinfo=UTC),
        ),
    )
    timeline.add(
        second_result,
        occurred_at=datetime(2026, 1, 12, tzinfo=UTC),
        validity=ValidityInterval(datetime(2026, 1, 12, tzinfo=UTC)),
    )
    first_derivation = lineage.add(experiment, first_result)
    lineage.add(first_result, second_result)
    versions.register(record_address, experiment)
    versions.append(record_address, first_result)
    versions.append(record_address, second_result)

    current = versions.current(record_address)
    temporal_item = timeline.get(experiment)
    catalog_item = catalog.get(catalog_address)
    lineage_path = lineage.shortest_path(experiment, second_result)
    graph_path = graph.shortest_path(experiment, first_result, relation="produced")
    run_items = catalog.items(namespace=("retrieval", "runs"))
    recent_events = events.tail(limit=2)
    january_events = timeline.events(
        start=datetime(2026, 1, 10, tzinfo=UTC),
        stop=datetime(2026, 1, 12, tzinfo=UTC),
    )
    current_facts = timeline.valid_at(datetime(2026, 1, 13, tzinfo=UTC))
    assert current is not None
    assert temporal_item is not None
    assert catalog_item is not None
    assert lineage_path is not None
    assert graph_path is not None
    occurred_at = temporal_item.entry.occurred_at
    assert occurred_at is not None

    print(f"ownership tree: experiment has {len(tree.children(experiment))} revisions")
    print(f"property graph: experiment --{produced.relation}--> first result")
    print(f"keyed collection: {len(run_items)} named runs")
    print(f"event log: latest sequences {[item.entry.sequence for item in recent_events]}")
    print(f"temporal collection: {len(january_events)} events before Jan 12")
    print(f"lineage DAG: experiment -> first result -> second result ({len(lineage_path)} edges)")
    print(f"versioned records: revision {current.entry.revision} -> {current.block.value}")

    assert tree.contains(experiment)
    assert tree.parent(first_result) == experiment
    assert tree.ancestors(first_result) == [experiment, workspace]
    assert tree.descendants(experiment) == [first_result, second_result]
    assert graph.edges(experiment, relation="produced") == [produced]
    assert graph.edges(experiment, relation="derived_into") == [first_derivation]
    assert graph_path.block_ids == (experiment.id, first_result.id)
    assert catalog.get(catalog_address) == experiment
    assert {item.block.id for item in run_items} == {first_result.id, second_result.id}
    assert events.get(0) is not None
    assert [item.block for item in recent_events] == [first_result, second_result]
    assert temporal_item.block == experiment
    assert {item.block.id for item in january_events} == {
        experiment.id,
        first_result.id,
    }
    assert [item.block for item in current_facts] == [second_result]
    assert lineage.successors(experiment)[0].block == first_result
    assert [item.entry.revision for item in versions.history(record_address)] == [1, 2]
    assert current.block == second_result

    # `LineageDAG` uses the backend's atomic acyclic-link capability.
    try:
        lineage.add(second_result, experiment)
    except MemoryConflict:
        print("lineage cycle: blocked as expected")
    else:
        raise AssertionError("A lineage DAG must reject cycles.")


if __name__ == "__main__":
    main()
