"""Tests for typed memory links and linked-memory search."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    InMemoryBackend,
    LinkRelations,
    Memory,
    MemoryLink,
    MemoryNotFound,
    MemoryPolicy,
)


def test_public_link_api_uses_explicit_operation_names() -> None:
    """The public API exposes add/list link names without old aliases."""

    memory = Memory()
    scope = memory.get_unbounded_handle()

    assert callable(memory.add_link)
    assert callable(memory.update_link)
    assert callable(memory.list_links)
    assert callable(memory.add_mount)
    assert callable(memory.list_mounts)
    assert callable(memory.discover_mounts)
    assert callable(memory.resolve_mount)
    assert callable(scope.add_link)
    assert callable(scope.update_link)
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


def test_link_metadata_requires_a_strict_json_dictionary() -> None:
    """Link creation and updates reject nonportable metadata."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="target")

    with pytest.raises(TypeError, match="dictionary"):
        memory.add_link(
            source,
            target,
            "related",
            metadata=cast(dict[str, Any], []),
        )
    link = memory.add_link(source, target, "related")
    with pytest.raises(TypeError, match="strict JSON"):
        memory.update_link(link, metadata={"confidence": float("inf")})


def test_update_link_merges_canonical_metadata_and_preserves_identity() -> None:
    """Link updates persist merged metadata without changing link identity."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="target")
    created = memory.add_link(
        source,
        target,
        "related",
        metadata={"valid_at": "2026-01-01", "evidence": {"episode": 1}},
    )

    updated = memory.update_link(created, metadata={"invalid_at": "2026-02-01"})
    updated_again = memory.update_link(created, metadata={"confidence": 0.9})

    assert updated is not created
    assert (
        updated.id,
        updated.source_id,
        updated.target_id,
        updated.relation,
        updated.created_at,
    ) == (
        created.id,
        created.source_id,
        created.target_id,
        created.relation,
        created.created_at,
    )
    assert created.metadata == {
        "valid_at": "2026-01-01",
        "evidence": {"episode": 1},
    }
    assert updated_again.metadata == {
        "valid_at": "2026-01-01",
        "invalid_at": "2026-02-01",
        "confidence": 0.9,
        "evidence": {"episode": 1},
    }
    updated_again.metadata["evidence"]["episode"] = 2
    assert memory.list_links(source)[0].metadata["evidence"] == {"episode": 1}


def test_update_link_rejects_missing_links() -> None:
    """Link updates require a canonical stored link rather than trusting input."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="target")
    forged = MemoryLink(source_id=source.id, target_id=target.id, relation="related")

    with pytest.raises(MemoryNotFound):
        memory.update_link(forged, metadata={"confidence": 0.9})


def test_update_link_requires_source_write_and_target_read_access() -> None:
    """Link updates enforce the same endpoint permissions as link creation."""

    owner = Memory(principal="owner")
    locked_source = owner.put(
        type="note",
        value="source",
        policy=MemoryPolicy(read=AccessRule.allow(), write=AccessRule.only(principals={"owner"})),
    )
    readable_target = owner.put(type="note", value="target", policy=MemoryPolicy.public())
    source_link = owner.add_link(locked_source, readable_target, "related")
    writable_source = owner.put(type="note", value="source", policy=MemoryPolicy.public())
    hidden_target = owner.put(
        type="note",
        value="target",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    target_link = owner.add_link(writable_source, hidden_target, "related")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        reader.update_link(source_link, metadata={"status": "updated"})
    with pytest.raises(AccessDenied):
        reader.update_link(target_link, metadata={"status": "updated"})


def test_update_link_requires_source_read_access() -> None:
    """A principal that can write but not read the source cannot update a link, because the stored link is returned."""

    owner = Memory(principal="owner")
    drop_box = MemoryPolicy(read=AccessRule.owner(), write=AccessRule.allow())
    write_only_source = owner.put(type="note", value="source", policy=drop_box)
    target = owner.put(type="note", value="target", policy=MemoryPolicy.public())
    link = owner.add_link(write_only_source, target, "related", metadata={"note": "private"})
    writer = Memory(backend=owner.backend, principal="writer")

    with pytest.raises(AccessDenied, match="cannot 'read'"):
        writer.update_link(link, metadata={"status": "updated"})

    assert owner.list_links(write_only_source)[0].metadata == {"note": "private"}


def test_scope_update_link_stays_inside_scope() -> None:
    """A scoped handle can update only regular links contained by its subtree."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    source = memory.put(parent=root, type="note", value="source")
    target = memory.put(parent=root, type="note", value="target")
    outside = memory.put(type="note", value="outside")
    inside_link = memory.add_link(source, target, "related", metadata={"status": "new"})
    outside_link = memory.add_link(source, outside, "related")
    scope = memory.get_handle(root=root)

    updated = scope.update_link(inside_link, metadata={"status": "reviewed"})

    assert updated.metadata == {"status": "reviewed"}
    with pytest.raises(AccessDenied):
        scope.update_link(outside_link, metadata={"status": "reviewed"})


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
    with pytest.raises(AccessDenied):
        plain_scope.update_link(mount, metadata={"label": "updated"})
    updated_mount = mount_scope.update_link(mount, metadata={"label": "updated"})
    resolved = mount_scope.resolve_mount(mount)

    assert mount_scope.list_mounts(mount_source) == [updated_mount]
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


def test_reserved_mount_relation_is_rejected_by_non_mount_operations() -> None:
    """Mount topology cannot enter the DAG API or be resolved from an ordinary link."""

    memory = Memory()
    source = memory.put(type="node", value="source")
    target = memory.put(type="node", value="target")
    ordinary = memory.add_link(source, target, "related")

    with pytest.raises(ValueError, match="reserved mount relation"):
        memory.add_acyclic_link(source, target, LinkRelations.MOUNT)
    with pytest.raises(ValueError, match="Expected mount relation"):
        memory.resolve_mount(ordinary)


def test_update_link_rejects_a_dangling_non_mount_target() -> None:
    """The memory layer detects a stale non-mount link returned by an adapter."""

    class DanglingLinkBackend(InMemoryBackend):
        """Simulate an adapter that failed to clean links during block deletion."""

        def delete_links_for_block(self, block_id: str) -> None:
            """Deliberately retain links so the public integrity check is exercised."""

    backend = DanglingLinkBackend()
    memory = Memory(backend=backend)
    source = memory.put(type="node", value="source")
    target = memory.put(type="node", value="target")
    link = memory.add_link(source, target, "related")
    memory.delete(target)

    with pytest.raises(MemoryNotFound, match="Target memory block"):
        memory.update_link(link, metadata={"stale": True})


def test_link_relations_and_endpoints_must_be_non_empty_strings() -> None:
    """Link calls reject a bad relation or endpoint before anything is stored."""

    memory = Memory()
    source = memory.put(type="note", value="source")
    target = memory.put(type="note", value="target")
    scope = memory.get_unbounded_handle(allow_mounts=True)

    for add in (memory.add_link, memory.add_acyclic_link, scope.add_link, scope.add_acyclic_link):
        with pytest.raises(TypeError, match="relation must be a string"):
            add(source, target, cast(str, None))
        with pytest.raises(ValueError, match="relation must not be empty"):
            add(source, target, "")
        with pytest.raises(TypeError, match="MemoryBlock or a string id"):
            add(source, cast(str, {"$ne": None}), "related")
    for add_mount in (memory.add_mount, scope.add_mount):
        with pytest.raises(TypeError, match="MemoryBlock or a string id"):
            add_mount(source, cast(str, 123))
    for list_links in (memory.list_links, scope.list_links):
        with pytest.raises(TypeError, match="relation must be a string"):
            list_links(source, relation=cast(str, {"$ne": None}))
        with pytest.raises(ValueError, match="relation must not be empty"):
            list_links(source, relation="")

    assert memory.list_links(source, direction="both") == []


def test_link_arguments_must_be_memory_links() -> None:
    """Update and mount resolution reject values that are not links."""

    memory = Memory()
    scope = memory.get_unbounded_handle(allow_mounts=True)

    for update_link in (memory.update_link, scope.update_link):
        with pytest.raises(TypeError, match="link must be a MemoryLink"):
            update_link(cast(MemoryLink, "link_missing"), metadata={})
    for resolve_mount in (memory.resolve_mount, scope.resolve_mount):
        with pytest.raises(TypeError, match="mount must be a MemoryLink"):
            resolve_mount(cast(MemoryLink, "link_missing"))


def test_memory_link_requires_string_endpoints_relation_and_id() -> None:
    """The link record rejects bad fields, including in copies."""

    link = MemoryLink(source_id="mem_a", target_id="mem_b", relation="related")

    with pytest.raises(TypeError, match="MemoryLink source_id must be a string"):
        MemoryLink(source_id=cast(str, 1), target_id="mem_b", relation="related")
    with pytest.raises(ValueError, match="MemoryLink target_id must not be empty"):
        MemoryLink(source_id="mem_a", target_id="", relation="related")
    with pytest.raises(TypeError, match="MemoryLink relation must be a string"):
        MemoryLink(source_id="mem_a", target_id="mem_b", relation=cast(str, None))
    with pytest.raises(ValueError, match="MemoryLink id must not be empty"):
        MemoryLink(source_id="mem_a", target_id="mem_b", relation="related", id="")
    with pytest.raises(TypeError, match="MemoryLink relation must be a string"):
        replace(link, relation=cast(str, None))
