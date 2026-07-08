"""Tests for typed memory links and linked-memory search."""

from __future__ import annotations

from typing import Any, cast

import pytest

from genie import AccessDenied, AccessRule, LinkRelations, Memory, MemoryLink, MemoryPolicy


def test_public_link_api_uses_explicit_operation_names() -> None:
    """The public API exposes add/list link names without old aliases."""

    memory = Memory()
    scope = memory.get_unbounded_handle()

    assert callable(memory.add_link)
    assert callable(memory.list_links)
    assert callable(memory.add_mount)
    assert callable(memory.list_mounts)
    assert callable(memory.discover_mounts)
    assert callable(memory.resolve_mount)
    assert callable(scope.add_link)
    assert callable(scope.list_links)
    assert callable(scope.add_mount)
    assert callable(scope.list_mounts)
    assert callable(scope.discover_mounts)
    assert callable(scope.resolve_mount)
    assert not hasattr(memory, "link")
    assert not hasattr(memory, "links")
    assert not hasattr(scope, "link")
    assert not hasattr(scope, "links")


def test_links_support_direction_filters_and_link_scope_search() -> None:
    """Links support direction filtering and linked-memory search."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="linked needle")
    inbound = memory.put(type="note", value="inbound")

    related = memory.add_link(source, target, "related", metadata={"weight": 0.8})
    incoming = memory.add_link(inbound, source, "mentions")

    assert memory.list_links(source, relation="related") == [related]
    assert memory.list_links(source, direction="in") == [incoming]
    assert sorted(link.id for link in memory.list_links(source, direction="both")) == sorted(
        [related.id, incoming.id]
    )

    results = memory.search("needle", root=source, scope="links")

    assert [result.block.id for result in results] == [target.id]


def test_link_metadata_is_defensively_copied() -> None:
    """Link metadata cannot be mutated through input or returned objects."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="target")
    metadata = {"tags": ["original"]}

    created = memory.add_link(source, target, "related", metadata=metadata)
    metadata["tags"].append("input-mutated")
    created.metadata["tags"].append("return-mutated")

    listed = memory.list_links(source)

    assert listed[0].metadata == {"tags": ["original"]}
    listed[0].metadata["tags"].append("listed-mutated")
    assert memory.list_links(source)[0].metadata == {"tags": ["original"]}


def test_list_links_hides_links_to_unreadable_endpoints() -> None:
    """Link listing hides links whose endpoints are unreadable."""

    owner = Memory(principal="owner")
    source = owner.put(type="note", value="source", policy=MemoryPolicy.public())
    target = owner.put(
        type="secret",
        value="hidden",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    link = owner.add_link(source, target, "mentions")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert owner.list_links(source) == [link]
    assert reader.list_links(source) == []


def test_scope_list_links_and_link_search_stay_inside_scope() -> None:
    """Scoped link listing and search do not expose out-of-scope blocks."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="note", value="inside")
    outside = memory.put(type="note", value="outside needle")
    link = memory.add_link(child, outside, "references")
    scope = memory.get_handle(root=root)

    assert memory.list_links(child) == [link]
    assert scope.list_links(child) == []
    assert [result.block.id for result in memory.search("outside", root=child, scope="links")] == [
        outside.id
    ]
    assert scope.search("outside", root=child, scope="links") == []


def test_links_reject_invalid_directions() -> None:
    """Invalid link directions are rejected."""

    memory = Memory()
    block = memory.put(type="note", value="source")

    with pytest.raises(ValueError):
        memory.list_links(block, direction=cast(Any, "sideways"))


def test_scope_linking_cannot_cross_scope_boundaries() -> None:
    """A scope cannot create links to blocks outside its subtree."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="note", value="child")
    outside = memory.put(type="note", value="outside")
    scope = memory.get_handle(root=root)

    with pytest.raises(AccessDenied):
        scope.add_link(child, outside, "related")


def test_mounts_are_symbolic_and_not_recursive_ownership() -> None:
    """Mounts resolve explicitly without changing subtree ownership."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    mount_source = memory.put(parent=local_root, type="reference", value="remote docs")
    remote_root = memory.put(type="project", value="remote")
    remote_child = memory.put(parent=remote_root, type="note", value="remote needle")

    mount = memory.add_mount(mount_source, remote_root, metadata={"label": "docs"})
    subtree_results = memory.search("remote needle", root=local_root, scope="subtree")
    link_results = memory.search("project", root=mount_source, scope="links")
    resolved = memory.resolve_mount(mount)

    assert mount.relation == LinkRelations.MOUNT
    assert memory.list_mounts(mount_source) == [mount]
    assert subtree_results == []
    assert [result.block.id for result in link_results] == [remote_root.id]
    assert resolved is not None
    assert resolved.id == remote_root.id

    memory.delete(local_root)

    assert memory.get(mount_source) is None
    assert memory.get(remote_root) is not None
    assert memory.get(remote_child) is not None


def test_discover_mounts_scans_subtree_without_following_targets() -> None:
    """Mount discovery finds local mount sources without entering mounted trees."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    local_child = memory.put(parent=local_root, type="task", value="local task")
    local_source = memory.put(parent=local_child, type="reference", value="remote docs")
    remote_root = memory.put(type="project", value="remote")
    remote_source = memory.put(parent=remote_root, type="reference", value="remote nested source")
    other_remote = memory.put(type="project", value="other remote")

    root_mount = memory.add_mount(local_root, remote_root, metadata={"label": "root mount"})
    child_mount = memory.add_mount(local_source, other_remote, metadata={"label": "child mount"})
    remote_mount = memory.add_mount(remote_source, other_remote)

    assert memory.discover_mounts(local_root) == [root_mount, child_mount]
    assert memory.discover_mounts(local_root, include_self=False) == [child_mount]
    assert remote_mount not in memory.discover_mounts(local_root)


def test_discover_mounts_hides_unreadable_sources_and_targets() -> None:
    """Mount discovery returns only links whose source and target are readable."""

    owner = Memory(principal="owner")
    root = owner.put(type="project", value="local", policy=MemoryPolicy.public())
    readable_source = owner.put(
        parent=root,
        type="reference",
        value="public source",
        policy=MemoryPolicy.public(),
    )
    hidden_source = owner.put(
        parent=root,
        type="reference",
        value="private source",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    visible_target = owner.put(type="project", value="public target", policy=MemoryPolicy.public())
    hidden_target = owner.put(
        type="project",
        value="private target",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )

    visible_mount = owner.add_mount(readable_source, visible_target)
    owner.add_mount(readable_source, hidden_target)
    owner.add_mount(hidden_source, visible_target)
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert reader.discover_mounts(root) == [visible_mount]


def test_scope_mounts_require_explicit_mount_access() -> None:
    """Scopes need explicit mount access before creating or resolving mounts."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    mount_source = memory.put(parent=local_root, type="reference", value="remote docs")
    remote_root = memory.put(type="project", value="remote")
    plain_scope = memory.get_handle(root=local_root)
    mount_scope = memory.get_handle(root=local_root, allow_mounts=True)

    with pytest.raises(AccessDenied):
        plain_scope.add_mount(mount_source, remote_root)
    with pytest.raises(AccessDenied):
        plain_scope.list_mounts(mount_source)

    mount = mount_scope.add_mount(mount_source, remote_root)
    resolved = mount_scope.resolve_mount(mount)

    assert mount_scope.list_mounts(mount_source) == [mount]
    assert resolved is not None
    assert resolved.id == remote_root.id
    with pytest.raises(AccessDenied):
        plain_scope.resolve_mount(mount)


def test_scope_discover_mounts_requires_mount_access_and_stays_in_scope() -> None:
    """Scoped mount discovery is explicit and cannot scan outside its root."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    local_child = memory.put(parent=local_root, type="task", value="local task")
    local_source = memory.put(parent=local_child, type="reference", value="remote docs")
    outside_source = memory.put(type="reference", value="outside source")
    remote_root = memory.put(type="project", value="remote")
    local_mount = memory.add_mount(local_source, remote_root)
    memory.add_mount(outside_source, remote_root)
    plain_scope = memory.get_handle(root=local_root)
    mount_scope = memory.get_handle(root=local_root, allow_mounts=True)
    child_scope = mount_scope.get_handle(root=local_child, allow_mounts=True)
    unbounded_handle = memory.get_unbounded_handle(allow_mounts=True)

    with pytest.raises(AccessDenied):
        plain_scope.discover_mounts()
    with pytest.raises(AccessDenied):
        mount_scope.discover_mounts(outside_source)
    with pytest.raises(AccessDenied):
        unbounded_handle.discover_mounts()

    assert mount_scope.discover_mounts() == [local_mount]
    assert child_scope.discover_mounts() == [local_mount]
    assert unbounded_handle.discover_mounts(local_root) == [local_mount]


def test_resolve_mount_requires_stored_mount_link() -> None:
    """Mount resolution does not trust a forged in-memory link object."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    mount_source = memory.put(parent=local_root, type="reference", value="remote docs")
    remote_root = memory.put(type="project", value="remote")
    scope = memory.get_handle(root=local_root, allow_mounts=True)
    forged = MemoryLink(
        source_id=mount_source.id,
        target_id=remote_root.id,
        relation=LinkRelations.MOUNT,
    )

    assert memory.resolve_mount(forged) is None
    assert scope.resolve_mount(forged) is None


def test_scope_mount_resolution_requires_local_mount_source() -> None:
    """A mount-enabled scope can resolve only mounts whose source is inside it."""

    memory = Memory()
    local_root = memory.put(type="project", value="local")
    memory.put(parent=local_root, type="reference", value="inside")
    outside_source = memory.put(type="reference", value="outside")
    remote_root = memory.put(type="project", value="remote")
    outside_mount = memory.add_mount(outside_source, remote_root)
    scope = memory.get_handle(root=local_root, allow_mounts=True)

    with pytest.raises(AccessDenied):
        scope.resolve_mount(outside_mount)
    with pytest.raises(AccessDenied):
        scope.list_mounts(outside_source)


def test_child_handles_cannot_add_mount_access() -> None:
    """Child handles cannot add mount access when the parent handle lacks it."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="task", value="child")
    parent_scope = memory.get_handle(root=root)
    mount_parent_scope = memory.get_handle(root=root, allow_mounts=True)

    with pytest.raises(AccessDenied):
        parent_scope.get_handle(root=child, allow_mounts=True)

    child_scope = mount_parent_scope.get_handle(root=child, allow_mounts=True)

    assert child_scope.allow_mounts is True
