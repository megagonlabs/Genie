"""Tests for the in-memory backend reference implementation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from genie import InMemoryBackend, Memory, MemoryBlock, MemoryLink, MemoryPolicy, MemoryQuery
from genie.memory.errors import MemoryConflict


def test_backends_can_be_shared_between_memory_instances() -> None:
    """Multiple Memory handles can share one backend."""

    backend = InMemoryBackend()
    writer = Memory(backend=backend, principal="writer")
    reader = Memory(backend=backend, principal="reader")

    block = writer.put(type="note", value="shared backend", policy=MemoryPolicy.public())

    retrieved = reader.get(block.id)
    assert retrieved is not None
    assert retrieved.value == "shared backend"


def test_backend_parent_moves_update_children_indexes() -> None:
    """Reparenting updates old and new child indexes."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    old_parent = memory.put(type="root", value="old")
    new_parent = memory.put(type="root", value="new")
    child = memory.put(parent=old_parent, type="child", value="child")

    raw_child = backend.get_block(child.id)
    assert raw_child is not None
    backend.update_block(raw_child.with_updates(parent_id=new_parent.id))

    assert backend.children(old_parent.id) == []
    assert [block.id for block in backend.children(new_parent.id)] == [child.id]


def test_backend_rejects_reparenting_cycles() -> None:
    """Reparenting rejects self-parenting and descendant cycles."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")

    raw_root = backend.get_block(root.id)
    raw_child = backend.get_block(child.id)
    assert raw_root is not None
    assert raw_child is not None

    with pytest.raises(MemoryConflict):
        backend.update_block(raw_root.with_updates(parent_id=child.id))
    with pytest.raises(MemoryConflict):
        backend.update_block(raw_child.with_updates(parent_id=child.id))


def test_backend_rejects_reparenting_to_missing_parent() -> None:
    """Reparenting to a missing parent leaves existing parent placement unchanged."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root")
    child = memory.put(parent=root, type="child", value="child")
    raw_child = backend.get_block(child.id)
    assert raw_child is not None

    with pytest.raises(MemoryConflict):
        backend.update_block(raw_child.with_updates(parent_id="missing"))

    assert [block.id for block in backend.children(root.id)] == [child.id]
    unchanged = backend.get_block(child.id)
    assert unchanged is not None
    assert unchanged.parent_id == root.id


def test_backend_rejects_a_stale_conditional_update() -> None:
    """In-memory replacement follows the shared compare-and-swap contract."""

    backend = InMemoryBackend()
    original = backend.put_block(MemoryBlock(id="note", type="note", value={}))
    first = backend.get_block(original.id)
    stale = backend.get_block(original.id)
    assert first is not None
    assert stale is not None

    backend.update_block(
        first.with_updates(value={"first": True}, version=2),
        expected_version=1,
    )
    with pytest.raises(MemoryConflict, match="changed"):
        backend.update_block(
            stale.with_updates(value={"stale": True}, version=2),
            expected_version=1,
        )


def test_backend_rejects_a_stale_conditional_subtree_delete() -> None:
    """In-memory deletion requires the complete supplied subtree snapshot."""

    backend = InMemoryBackend()
    root = backend.put_block(MemoryBlock(id="root", type="root", value={}))
    snapshot = {root.id: root.version}
    child = backend.put_block(
        MemoryBlock(id="child", parent_id=root.id, type="child", value={})
    )

    with pytest.raises(MemoryConflict, match="changed"):
        backend.delete_block(root.id, expected_versions=snapshot)

    assert backend.get_block(root.id) is not None
    assert backend.get_block(child.id) is not None


def test_backend_contains_requires_existing_root() -> None:
    """Containment returns false when the potential root does not exist."""

    backend = InMemoryBackend()

    assert backend.contains("missing", "missing") is False


# `id: parent_id` for a tree with two roots, a fork, and three depths.
_TREE = {
    "root": None,
    "a": "root",
    "a1": "a",
    "a2": "a",
    "a2x": "a2",
    "b": "root",
    "b1": "b",
    "other": None,
    "other1": "other",
}


def _build_tree(backend: InMemoryBackend) -> None:
    """Write `_TREE` parents first, so a block never precedes its parent."""

    for block_id, parent_id in _TREE.items():
        backend.put_block(MemoryBlock(id=block_id, parent_id=parent_id, type="node", value={}))


def _descendant_scan(backend: InMemoryBackend, root_id: str, block_id: str) -> bool:
    """Answer containment by scanning the subtree, the slow but obvious way."""

    if backend.get_block(root_id) is None:
        return False
    if root_id == block_id:
        return True
    return any(block.id == block_id for block in backend.descendants(root_id))


def test_backend_contains_agrees_with_a_descendant_scan_on_every_pair() -> None:
    """The upward walk answers exactly what a full subtree scan would answer."""

    backend = InMemoryBackend()
    _build_tree(backend)
    candidates = [*_TREE, "missing"]

    for root_id in candidates:
        for block_id in candidates:
            assert backend.contains(root_id, block_id) is _descendant_scan(
                backend, root_id, block_id
            ), f"disagreed on contains({root_id!r}, {block_id!r})"


def test_backend_contains_answers_in_one_direction_only() -> None:
    """Containment follows ownership downward, never back up the tree."""

    backend = InMemoryBackend()
    _build_tree(backend)

    assert backend.contains("root", "a2x") is True
    assert backend.contains("a", "a2x") is True
    assert backend.contains("a2", "a2x") is True
    assert backend.contains("a2x", "a2x") is True

    assert backend.contains("a2x", "root") is False
    assert backend.contains("a2", "a") is False


def test_backend_contains_keeps_separate_trees_apart() -> None:
    """A second root owns nothing in the first root's subtree."""

    backend = InMemoryBackend()
    _build_tree(backend)

    assert backend.contains("other", "other1") is True
    assert backend.contains("other", "a1") is False
    assert backend.contains("root", "other1") is False
    assert backend.contains("a", "b1") is False


def test_backend_contains_walks_a_deep_chain() -> None:
    """Depth is the cost of the check, so a long chain must still be answered."""

    backend = InMemoryBackend()
    parent_id = None
    ids = []
    for depth in range(200):
        block_id = f"level_{depth:03d}"
        backend.put_block(MemoryBlock(id=block_id, parent_id=parent_id, type="node", value={}))
        ids.append(block_id)
        parent_id = block_id

    assert backend.contains(ids[0], ids[-1]) is True
    assert backend.contains(ids[-1], ids[0]) is False
    assert backend.contains(ids[100], ids[-1]) is True
    assert backend.contains(ids[-1], ids[100]) is False


def test_backend_contains_reports_a_missing_candidate_as_absent() -> None:
    """An id that was never stored is not inside any subtree."""

    backend = InMemoryBackend()
    _build_tree(backend)

    assert backend.contains("root", "missing") is False
    assert backend.contains("missing", "root") is False


def test_backend_contains_follows_a_reparented_block() -> None:
    """Moving a block moves the whole branch it owns to the new subtree."""

    backend = InMemoryBackend()
    _build_tree(backend)
    moving = backend.get_block("a2")
    assert moving is not None

    backend.update_block(moving.with_updates(parent_id="b", version=moving.version + 1))

    assert backend.contains("b", "a2") is True
    assert backend.contains("b", "a2x") is True
    assert backend.contains("a", "a2") is False
    assert backend.contains("a", "a2x") is False
    assert backend.contains("root", "a2x") is True


def test_backend_rejects_moving_a_block_under_its_own_descendant() -> None:
    """`contains` is the cycle guard, so the rejection must keep working."""

    backend = InMemoryBackend()
    _build_tree(backend)
    parent = backend.get_block("a")
    assert parent is not None

    with pytest.raises(MemoryConflict, match="cannot be moved under one of its descendants"):
        backend.update_block(parent.with_updates(parent_id="a2x", version=parent.version + 1))

    assert backend.contains("a", "a2x") is True
    assert backend.contains("a2x", "a") is False


def test_backend_rejects_making_a_block_its_own_parent() -> None:
    """A self parent would be a one-block cycle."""

    backend = InMemoryBackend()
    _build_tree(backend)
    block = backend.get_block("a")
    assert block is not None

    with pytest.raises(MemoryConflict, match="cannot be its own parent"):
        backend.update_block(block.with_updates(parent_id="a", version=block.version + 1))


def test_backend_links_return_defensive_metadata_copies() -> None:
    """Backend link reads cannot mutate stored link metadata."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    source = memory.put(type="source", value="source")
    target = memory.put(type="target", value="target")
    link = backend.add_link(
        MemoryLink(
            source_id=source.id,
            target_id=target.id,
            relation="references",
            metadata={"tags": ["initial"]},
        )
    )

    link.metadata["tags"].append("returned")
    listed = backend.list_links(source.id)
    listed[0].metadata["tags"].append("listed")

    relisted = backend.list_links(source.id)
    assert relisted[0].metadata == {"tags": ["initial"]}


def test_backend_updates_only_existing_link_metadata() -> None:
    """Backend link updates preserve identity and reject implicit overwrites."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    source = memory.put(type="source", value="source")
    target = memory.put(type="target", value="target")
    link = backend.add_link(
        MemoryLink(
            source_id=source.id,
            target_id=target.id,
            relation="references",
            metadata={"status": "new"},
        )
    )

    updated = backend.update_link(replace(link, metadata={"status": "reviewed"}))

    assert updated.metadata == {"status": "reviewed"}
    assert backend.list_links(source.id) == [updated]
    with pytest.raises(MemoryConflict):
        backend.update_link(replace(link, relation="replaced"))
    with pytest.raises(MemoryConflict):
        backend.update_link(MemoryLink(source_id=source.id, target_id=target.id, relation="missing"))
    with pytest.raises(MemoryConflict):
        backend.add_link(link)


def test_backend_can_delete_links_for_a_block() -> None:
    """Backends expose explicit cleanup for links connected to a block."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    source = memory.put(type="source", value="source")
    target = memory.put(type="target", value="target")
    incoming = memory.put(type="incoming", value="incoming")
    backend.add_link(MemoryLink(source_id=source.id, target_id=target.id, relation="out"))
    backend.add_link(MemoryLink(source_id=incoming.id, target_id=source.id, relation="in"))

    backend.delete_links_for_block(source.id)

    assert backend.list_links(source.id, direction="both") == []
    assert backend.list_links(target.id, direction="both") == []
    assert backend.list_links(incoming.id, direction="both") == []


def test_inmemory_backend_read_paths_return_defensive_copies() -> None:
    """Backend reads, traversal, and search return defensive copies."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(
        type="root",
        value={"items": ["root"]},
        metadata={"tags": ["root"]},
    )
    child = memory.put(
        parent=root,
        type="note",
        value={"items": ["child"]},
        metadata={"tags": ["child"]},
    )

    fetched = backend.get_block(root.id)
    assert fetched is not None
    fetched.value["items"].append("mutated")
    fetched.metadata["tags"].append("mutated")
    refetched = backend.get_block(root.id)
    assert refetched is not None
    assert refetched.value == {"items": ["root"]}
    assert refetched.metadata == {"tags": ["root"]}

    child_from_children = backend.children(root.id)[0]
    child_from_children.value["items"].append("children-mutated")
    child_after_children = backend.get_block(child.id)
    assert child_after_children is not None
    assert child_after_children.value == {"items": ["child"]}

    child_from_descendants = backend.descendants(root.id, include_self=True)[1]
    child_from_descendants.metadata["tags"].append("descendants-mutated")
    child_after_descendants = backend.get_block(child.id)
    assert child_after_descendants is not None
    assert child_after_descendants.metadata == {"tags": ["child"]}

    result = backend.search(MemoryQuery(text="child"))[0]
    result.block.value["items"].append("search-mutated")
    child_after_search = backend.get_block(child.id)
    assert child_after_search is not None
    assert child_after_search.value == {"items": ["child"]}


def test_inmemory_link_scope_search_has_deterministic_tie_order() -> None:
    """Linked candidates preserve first-seen link endpoint order."""

    backend = InMemoryBackend()
    root = backend.put_block(MemoryBlock(id="root", type="node", value={}))
    first = backend.put_block(MemoryBlock(id="first", type="node", value={}))
    second = backend.put_block(MemoryBlock(id="second", type="node", value={}))
    backend.add_link(MemoryLink(root.id, second.id, "related", id="first-link"))
    backend.add_link(MemoryLink(first.id, root.id, "related", id="second-link"))

    result_ids = [
        result.block.id
        for result in backend.search(
            MemoryQuery(root_id=root.id, scope="links")
        )
    ]

    assert result_ids == [root.id, second.id, first.id]
