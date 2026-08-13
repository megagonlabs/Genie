"""Compose all seven optional structures around the same memory blocks.

Run from the repository root:

    conda run -n "genie" python examples/advanced/experiment_memory.py
"""

from __future__ import annotations

from datetime import UTC, datetime

if __package__ in {None, ""}:
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory
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
    """Use one experiment record in every optional structure."""

    print("== Experiment memory using all seven structures ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    workspace = memory.put(type="workspace", value="retrieval experiments")
    handle = memory.get_handle(root=workspace)
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
        type="experiment_revision",
        value={"candidate_count": 20, "recall": 0.78},
    )
    second_result = tree.create_child(
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
    events.append(experiment)
    timeline.add(
        experiment,
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        validity=ValidityInterval(datetime(2026, 1, 10, tzinfo=UTC)),
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
    assert current is not None
    assert temporal_item is not None
    assert catalog_item is not None
    assert lineage_path is not None
    occurred_at = temporal_item.entry.occurred_at
    assert occurred_at is not None

    print("ownership tree: experiment is inside the workspace")
    print(f"property graph: experiment --{produced.relation}--> first result")
    print(f"keyed collection: retrieval/current_experiment -> {catalog_item.value}")
    print(f"event log: {len(events.read())} experiment event")
    print(f"temporal collection: experiment occurred on {occurred_at.date()}")
    print(f"lineage DAG: experiment -> first result -> second result ({len(lineage_path)} edges)")
    print(f"versioned records: revision {current.entry.revision} -> {current.block.value}")

    assert tree.contains(experiment)
    assert graph.edges(experiment, relation="produced") == [produced]
    assert graph.edges(experiment, relation="derived_into") == [first_derivation]
    assert catalog.get(catalog_address) == experiment
    assert events.read()[0].block == experiment
    assert temporal_item.block == experiment
    assert lineage.successors(experiment)[0].block == first_result
    assert [item.entry.revision for item in versions.history(record_address)] == [1, 2]
    assert current.block == second_result


if __name__ == "__main__":
    main()
