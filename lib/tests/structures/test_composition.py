"""Tests that use several optional structure facades over the same blocks."""

from __future__ import annotations

from datetime import UTC, datetime

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


def test_one_block_can_participate_in_all_seven_structures() -> None:
    """The 7 structures refer to the same block and do not copy its value."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="workspace", value="demo")
    tree = OwnershipTree(memory, root=root)
    catalog_root = tree.create_child(type="collection", value="catalog")
    events_root = tree.create_child(type="log", value="events")
    timeline_root = tree.create_child(type="timeline", value="timeline")
    versions_root = tree.create_child(type="versions", value="records")
    identity = tree.create_child(
        type="record_identity",
        value={"name": "experiment"},
    )
    revision = tree.create_child(
        type="record_revision",
        value={"result": "passed"},
    )

    graph = PropertyGraph(memory)
    keyed = KeyedCollection(memory, backend, collection=catalog_root)
    events = EventLog(memory, backend, log=events_root)
    timeline = TemporalCollection(memory, backend, collection=timeline_root)
    lineage = LineageDAG(memory, relation="derived_into")
    versions = VersionedRecords(memory, backend, collection=versions_root)
    catalog_address = MemoryAddress(("experiments",), "current")
    record_address = MemoryAddress(("experiments",), "result")

    graph.add_edge(identity, revision, "supports")
    keyed.bind(catalog_address, identity)
    events.append(identity)
    timeline.add(
        identity,
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        validity=ValidityInterval(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    lineage.add(identity, revision)
    versions.register(record_address, identity)
    versions.append(record_address, revision)

    assert tree.contains(identity)
    assert graph.neighbors(identity, relation="supports")[0].block == revision
    assert keyed.get(catalog_address) == identity
    assert events.read()[0].block == identity
    temporal_item = timeline.get(identity)
    assert temporal_item is not None
    assert temporal_item.block == identity
    assert lineage.successors(identity)[0].block == revision
    assert versions.identity(record_address) == identity
    current = versions.current(record_address)
    assert current is not None
    assert current.block == revision
    assert memory.get(identity) == identity

    memory.delete(identity)

    assert not tree.contains(identity)
    assert graph.edges(revision, direction="both") == []
    assert keyed.get(catalog_address) is None
    assert events.read() == []
    assert timeline.get(identity.id) is None
    assert lineage.predecessors(revision) == []
    assert versions.identity(record_address) is None
    assert versions.current(record_address) is None
    assert memory.get(revision) == revision


def test_versioned_records_interoperate_with_keyed_collection_and_event_log() -> None:
    """`VersionedRecords` and its 2 base facades, `KeyedCollection` and `EventLog`, share the same state."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    collection_root = memory.put(type="versions", value="records")
    identity = memory.put(type="record_identity", value="favorite color")
    blue = memory.put(type="record_revision", value="blue")
    green = memory.put(type="record_revision", value="green")
    address = MemoryAddress(("users", "alice"), "favorite_color")
    versions = VersionedRecords(memory, backend, collection=collection_root)

    versions.register(address, identity)
    first = versions.append(address, blue)

    identities = KeyedCollection(memory, backend, collection=collection_root)
    revisions = EventLog(memory, backend, log=identity)
    assert identities.get(address) == identity
    assert revisions.read()[0].block == first.block
    assert revisions.read()[0].entry.sequence == first.entry.revision - 1

    raw_second = revisions.append(green)

    second = versions.revision(address, 2)
    assert second is not None
    assert second.block == raw_second.block
    assert second.entry.record_id == raw_second.entry.log_id
    assert second.entry.revision == raw_second.entry.sequence + 1
    assert versions.current(address) == second
