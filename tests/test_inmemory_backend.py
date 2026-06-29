from __future__ import annotations

import pytest

from genie import InMemoryBackend, Memory, MemoryQuery
from genie.memory.errors import MemoryConflict


def test_backends_can_be_shared_between_memory_instances() -> None:
    backend = InMemoryBackend()
    writer = Memory(backend=backend, principal="writer")
    reader = Memory(backend=backend, principal="reader")

    block = writer.put(type="note", value="shared backend")

    retrieved = reader.get(block.id)
    assert retrieved is not None
    assert retrieved.value == "shared backend"


def test_backend_parent_moves_update_children_indexes() -> None:
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


def test_inmemory_backend_read_paths_return_defensive_copies() -> None:
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
