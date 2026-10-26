"""Scope and policy tests for optional structures that work together."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    HandleCapability,
    HookContext,
    HookOperation,
    InMemoryBackend,
    Memory,
    MemoryHook,
    MemoryPolicy,
    Operation,
)
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


class _DenialHook(MemoryHook):
    """Record every denial that `on_denied` reports."""

    def __init__(self) -> None:
        self.denials: list[tuple[HookOperation, str, str | None]] = []

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
    ) -> None:
        """Record the attempted operation, the check that denied it, and the subject."""

        self.denials.append((context.operation, str(operation), subject_id))


def test_compound_facades_apply_one_handle_scope_and_block_policy() -> None:
    """Every facade over one handle omits hidden blocks and blocks outside the scope in the same way."""

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
    owner.protect_relation("derived_into")
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
    memory.protect_relation("derived_into")
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


def test_membership_changes_require_write_on_the_container() -> None:
    """A principal that can read a container but not write it cannot change its members."""

    backend = InMemoryBackend()
    hook = _DenialHook()
    owner = Memory(backend=backend, principal="agent:owner")
    read_only = MemoryPolicy(read=AccessRule.allow(), search=AccessRule.allow(), traverse_up=AccessRule.allow())
    catalog_root = owner.put(type="collection", value="catalog", policy=read_only)
    events_root = owner.put(type="log", value="events", policy=read_only)
    timeline_root = owner.put(type="timeline", value="timeline", policy=read_only)
    versions_root = owner.put(type="versions", value="versions", policy=read_only)
    identity = owner.put(type="identity", value="record", policy=read_only)
    good = owner.put(type="fact", value="good", policy=read_only)
    address = MemoryAddress((), "prod")
    when = datetime(2026, 1, 1, tzinfo=UTC)
    KeyedCollection(owner, backend, collection=catalog_root).bind(address, good)
    EventLog(owner, backend, log=events_root).append(good)
    TemporalCollection(owner, backend, collection=timeline_root).add(good, occurred_at=when)
    owner_versions = VersionedRecords(owner, backend, collection=versions_root)
    owner_versions.register(address, identity)
    first_revision = owner_versions.append(address, good)

    reader = Memory(backend=backend, principal="agent:reader", hooks=[hook])
    mine = reader.put(type="fact", value="reader's", policy=MemoryPolicy.public())
    keyed = KeyedCollection(reader, backend, collection=catalog_root)
    events = EventLog(reader, backend, log=events_root)
    timeline = TemporalCollection(reader, backend, collection=timeline_root)
    versions = VersionedRecords(reader, backend, collection=versions_root)
    attempts = {
        "bind": lambda: keyed.bind(MemoryAddress((), "new"), mine),
        "rebind": lambda: keyed.rebind(address, mine, expected=good),
        "unbind": lambda: keyed.unbind(address, expected=good),
        "append": lambda: events.append(mine),
        "add": lambda: timeline.add(mine, occurred_at=when),
        "replace": lambda: timeline.replace(good, occurred_at=when),
        "remove": lambda: timeline.remove(good),
        "register": lambda: versions.register(MemoryAddress((), "new"), mine),
        "append revision": lambda: versions.append(address, mine),
    }

    for attempt in attempts.values():
        with pytest.raises(AccessDenied):
            attempt()

    assert [operation for operation, _, _ in hook.denials] == [HookOperation.REQUIRE_ACCESS] * len(attempts)
    assert {check for _, check, _ in hook.denials} == {Operation.WRITE}
    assert [subject for _, _, subject in hook.denials] == [
        catalog_root.id,
        catalog_root.id,
        catalog_root.id,
        events_root.id,
        timeline_root.id,
        timeline_root.id,
        timeline_root.id,
        versions_root.id,
        identity.id,
    ]
    assert keyed.get(address) == good
    assert [item.block for item in events.read()] == [good]
    assert [item.block for item in timeline.events()] == [good]
    assert versions.current(address) == first_revision


def test_handle_membership_changes_require_write_on_the_container() -> None:
    """A facade over a handle applies the container `write` rule through the handle."""

    backend = InMemoryBackend()
    hook = _DenialHook()
    owner = Memory(backend=backend, principal="agent:owner", hooks=[hook])
    read_only = MemoryPolicy(read=AccessRule.allow())
    root = owner.put(type="workspace", value="root", policy=read_only)
    catalog_root = owner.put(parent=root, type="collection", value="catalog", policy=read_only)
    events_root = owner.put(parent=root, type="log", value="events", policy=read_only)
    fact = owner.put(parent=root, type="fact", value="fact", policy=read_only)
    handle = owner.get_handle(root=root, principal="agent:reader")
    keyed = KeyedCollection(handle, backend, collection=catalog_root)
    events = EventLog(handle, backend, log=events_root)

    with pytest.raises(AccessDenied):
        keyed.bind(MemoryAddress((), "fact"), fact)
    with pytest.raises(AccessDenied):
        events.append(fact)

    assert hook.denials == [
        (HookOperation.REQUIRE_ACCESS, Operation.WRITE, catalog_root.id),
        (HookOperation.REQUIRE_ACCESS, Operation.WRITE, events_root.id),
    ]
    assert keyed.items() == []
    assert events.read() == []
