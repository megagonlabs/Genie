"""Tests for typed memory links and linked-memory search."""

from __future__ import annotations

from typing import Any, cast

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryPolicy


def test_public_link_api_uses_explicit_operation_names() -> None:
    """The public API exposes add/list link names without old aliases."""

    memory = Memory()
    scope = memory.scope()

    assert callable(memory.add_link)
    assert callable(memory.list_links)
    assert callable(scope.add_link)
    assert callable(scope.list_links)
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
    source = owner.put(type="note", value="source")
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
    scope = memory.scope(root=root)

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
    scope = memory.scope(root=root)

    with pytest.raises(AccessDenied):
        scope.add_link(child, outside, "related")
