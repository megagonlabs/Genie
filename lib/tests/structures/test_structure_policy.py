"""Composition-level scope and policy tests for optional structures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genie import AccessDenied, InMemoryBackend, Memory, MemoryPolicy
from genie.structures import (
    EventLog,
    KeyedCollection,
    LineageDAG,
    MemoryAddress,
    OwnershipTree,
    PropertyGraph,
    TemporalCollection,
    VersionedRecords,
)


def test_compound_facades_apply_one_handle_scope_and_block_policy() -> None:
    """A compound view consistently omits hidden and out-of-scope blocks."""

    backend = InMemoryBackend()
    owner = Memory(backend=backend, principal="agent:owner")
    public = MemoryPolicy.public()
    root = owner.put(type="workspace", value="root", policy=public)
    visible = owner.put(parent=root, type="fact", value="visible", policy=public)
    hidden = owner.put(parent=root, type="fact", value="hidden")
    outside = owner.put(type="fact", value="outside", policy=public)
    graph_target = owner.put(parent=root, type="fact", value="target", policy=public)
    catalog_root = owner.put(parent=root, type="collection", value="catalog", policy=public)
    events_root = owner.put(parent=root, type="log", value="events", policy=public)
    timeline_root = owner.put(parent=root, type="timeline", value="timeline", policy=public)
    versions_root = owner.put(parent=root, type="versions", value="versions", policy=public)
    identity = owner.put(parent=root, type="identity", value="record", policy=public)
    visible_revision = owner.put(parent=root, type="revision", value="visible", policy=public)
    hidden_revision = owner.put(parent=root, type="revision", value="hidden")
    address = MemoryAddress((), "record")

    graph = PropertyGraph(owner)
    graph.add_edge(visible, graph_target, "related")
    graph.add_edge(visible, hidden, "related")
    graph.add_edge(visible, outside, "related")
    lineage = LineageDAG(owner, relation="derived_into")
    lineage.add(visible, graph_target)
    lineage.add(visible, hidden)
    keyed = KeyedCollection(owner, backend, collection=catalog_root)
    keyed.bind(MemoryAddress((), "visible"), visible)
    keyed.bind(MemoryAddress((), "hidden"), hidden)
    keyed.bind(MemoryAddress((), "outside"), outside)
    events = EventLog(owner, backend, log=events_root)
    visible_log_item = events.append(visible)
    hidden_log_item = events.append(hidden)
    outside_log_item = events.append(outside)
    timeline = TemporalCollection(owner, backend, collection=timeline_root)
    for day, block in enumerate((visible, hidden, outside), start=1):
        timeline.add(block, occurred_at=datetime(2026, 1, day, tzinfo=UTC))
    versions = VersionedRecords(owner, backend, collection=versions_root)
    versions.register(address, identity)
    visible_version = versions.append(address, visible_revision)
    hidden_version = versions.append(address, hidden_revision)
    versions.append(address, outside)

    handle = owner.get_handle(root=root, principal="agent:viewer")
    scoped_tree = OwnershipTree(handle, root=root)
    scoped_graph = PropertyGraph(handle)
    scoped_lineage = LineageDAG(handle, relation="derived_into")
    scoped_keyed = KeyedCollection(handle, backend, collection=catalog_root)
    scoped_events = EventLog(handle, backend, log=events_root)
    scoped_timeline = TemporalCollection(handle, backend, collection=timeline_root)
    scoped_versions = VersionedRecords(handle, backend, collection=versions_root)

    assert hidden not in scoped_tree.descendants()
    assert outside not in scoped_tree.descendants()
    assert [item.block for item in scoped_graph.neighbors(visible, relation="related")] == [graph_target]
    assert [item.block for item in scoped_lineage.successors(visible)] == [graph_target]
    assert [item.block for item in scoped_keyed.items()] == [visible]
    assert [item.block for item in scoped_events.read()] == [visible]
    assert [item.block for item in scoped_timeline.events()] == [visible]
    assert scoped_versions.history(address) == [visible_version]
    assert scoped_versions.current(address) == visible_version

    with pytest.raises(AccessDenied):
        scoped_keyed.get(MemoryAddress((), "hidden"))
    with pytest.raises(AccessDenied):
        scoped_events.get(hidden_log_item.entry.sequence)
    with pytest.raises(AccessDenied):
        scoped_events.get(outside_log_item.entry.sequence)
    with pytest.raises(AccessDenied):
        scoped_versions.revision(address, hidden_version.entry.revision)
    exact_visible = scoped_events.get(visible_log_item.entry.sequence)
    assert exact_visible is not None
    assert exact_visible.block == visible


def test_compound_structural_writes_cannot_escape_a_handle() -> None:
    """Every facade resolves referenced blocks through its bounded handle."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="workspace", value="root")
    inside = memory.put(parent=root, type="item", value="inside")
    outside = memory.put(type="item", value="outside")
    catalog_root = memory.put(parent=root, type="collection", value="catalog")
    events_root = memory.put(parent=root, type="log", value="events")
    timeline_root = memory.put(parent=root, type="timeline", value="timeline")
    versions_root = memory.put(parent=root, type="versions", value="versions")
    identity = memory.put(parent=root, type="identity", value="record")
    address = MemoryAddress((), "record")
    VersionedRecords(memory, backend, collection=versions_root).register(address, identity)
    handle = memory.get_handle(root=root)

    tree = OwnershipTree(handle, root=root)
    graph = PropertyGraph(handle)
    lineage = LineageDAG(handle, relation="derived_into")
    keyed = KeyedCollection(handle, backend, collection=catalog_root)
    events = EventLog(handle, backend, log=events_root)
    timeline = TemporalCollection(handle, backend, collection=timeline_root)
    versions = VersionedRecords(handle, backend, collection=versions_root)

    with pytest.raises(AccessDenied):
        tree.create_child(parent=outside, type="item", value="escape")
    with pytest.raises(AccessDenied):
        graph.add_edge(inside, outside, "related")
    with pytest.raises(AccessDenied):
        lineage.add(inside, outside)
    with pytest.raises(AccessDenied):
        keyed.bind(MemoryAddress((), "escape"), outside)
    with pytest.raises(AccessDenied):
        events.append(outside)
    with pytest.raises(AccessDenied):
        timeline.add(outside, occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(AccessDenied):
        versions.append(address, outside)

    assert memory.list_links(inside) == []
    assert KeyedCollection(memory, backend, collection=catalog_root).items() == []
    assert EventLog(memory, backend, log=events_root).read() == []
    assert TemporalCollection(memory, backend, collection=timeline_root).get(outside) is None
    assert VersionedRecords(memory, backend, collection=versions_root).history(address) == []
