"""Tests for namespaced keyed collections and their atomic backend capability."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from genie import (
    AccessDenied,
    InMemoryBackend,
    KeyedStore,
    Memory,
    MemoryConflict,
    MemoryNotFound,
    UnsupportedOperation,
)
from genie.structures import (
    KeyedAccess,
    KeyedCollection,
    MemoryAddress,
)


def test_keyed_collection_binds_and_resolves_exact_addresses() -> None:
    """A collection maps one namespaced address to an existing memory block."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    language = memory.put(type="preference", value="English")
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress(("users", "alice"), "language")

    item = collection.bind(address, language)

    assert item.binding.collection_id == root.id
    assert item.binding.address == address
    assert item.block == language
    assert collection.get(address) == language
    assert collection.get_item(address) == item
    assert collection.get(MemoryAddress(("users", "alice"), "missing")) is None


def test_keyed_collection_lists_exact_and_recursive_namespaces() -> None:
    """Namespace listing is stable and can use an exact namespace or prefix."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    collection = KeyedCollection(memory, backend, collection=root)
    alice_language = memory.put(type="preference", value="English")
    alice_seat = memory.put(type="preference", value="aisle")
    bob_language = memory.put(type="preference", value="French")
    root_value = memory.put(type="preference", value="shared")
    collection.bind(MemoryAddress(("users", "bob"), "language"), bob_language)
    collection.bind(MemoryAddress((), "shared"), root_value)
    collection.bind(
        MemoryAddress(("users", "alice", "travel"), "seat"),
        alice_seat,
    )
    collection.bind(MemoryAddress(("users", "alice"), "language"), alice_language)

    assert [item.block.value for item in collection.items()] == [
        "shared",
        "English",
        "aisle",
        "French",
    ]
    assert [
        item.block.value
        for item in collection.items(namespace=("users", "alice"))
    ] == ["English"]
    assert [
        item.block.value
        for item in collection.items(
            namespace=("users", "alice"),
            recursive=True,
        )
    ] == ["English", "aisle"]
    assert len(collection.items(limit=2)) == 2


def test_keyed_collection_uses_atomic_compare_and_swap_updates() -> None:
    """Rebind and unbind fail when another value owns the expected address."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    blue = memory.put(type="preference", value="blue")
    green = memory.put(type="preference", value="green")
    purple = memory.put(type="preference", value="purple")
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress(("users", "alice"), "favorite_color")
    collection.bind(address, blue)

    with pytest.raises(MemoryConflict, match="already bound"):
        collection.bind(address, green)

    updated = collection.rebind(address, green, expected=blue)
    assert updated.block == green
    with pytest.raises(MemoryConflict, match="expected block"):
        collection.rebind(address, purple, expected=blue)
    with pytest.raises(MemoryConflict, match="expected block"):
        collection.unbind(address, expected=blue)

    collection.unbind(address, expected=green)
    assert collection.get(address) is None
    assert memory.get(green.id) == green


def test_same_address_is_independent_between_collections() -> None:
    """Address uniqueness is scoped by collection id."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    first_root = memory.put(type="collection", value="first")
    second_root = memory.put(type="collection", value="second")
    first_value = memory.put(type="value", value="first")
    second_value = memory.put(type="value", value="second")
    first = KeyedCollection(memory, backend, collection=first_root)
    second = KeyedCollection(memory, backend, collection=second_root)
    address = MemoryAddress((), "same")

    first.bind(address, first_value)
    second.bind(address, second_value)

    assert first.get(address) == first_value
    assert second.get(address) == second_value


def test_keyed_collection_reads_across_bounded_backend_pages() -> None:
    """The facade fills its visible limit across multiple store pages."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="values")
    collection = KeyedCollection(memory, backend, collection=root)
    for index in range(125):
        block = memory.put(type="value", value=index)
        collection.bind(MemoryAddress((), f"key-{index:03d}"), block)

    items = collection.items(limit=125)

    assert len(items) == 125
    assert [item.block.value for item in items] == list(range(125))


def test_keyed_collection_preserves_handle_scope() -> None:
    """Exact lookup propagates denial while listing omits inaccessible targets."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    inside = memory.put(parent=root, type="preference", value="inside")
    outside = memory.put(type="preference", value="outside")
    full = KeyedCollection(memory, backend, collection=root)
    inside_address = MemoryAddress((), "inside")
    outside_address = MemoryAddress((), "outside")
    full.bind(inside_address, inside)
    full.bind(outside_address, outside)
    scoped = KeyedCollection(memory.get_handle(root=root), backend, collection=root)

    assert [item.block for item in scoped.items()] == [inside]
    assert scoped.get(inside_address) == inside
    with pytest.raises(AccessDenied):
        scoped.get(outside_address)
    with pytest.raises(AccessDenied):
        scoped.bind(MemoryAddress((), "escape"), outside)


def test_key_bindings_are_cleaned_when_blocks_are_deleted() -> None:
    """Deleting a target or collection removes its dependent binding state."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    value = memory.put(type="preference", value="value")
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress((), "key")
    collection.bind(address, value)

    memory.delete(value)
    assert collection.get(address) is None

    replacement = memory.put(type="preference", value="replacement")
    collection.bind(address, replacement)
    memory.delete(root)
    assert backend.get_key(root.id, address) is None
    with pytest.raises(MemoryNotFound):
        _ = collection.collection


def test_key_binding_is_atomic_under_concurrent_writers() -> None:
    """Only one concurrent create-only bind can claim an address."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    values = [memory.put(type="value", value=index) for index in range(20)]
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress((), "winner")

    def try_bind(index: int) -> bool:
        try:
            collection.bind(address, values[index])
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(try_bind, range(len(values))))

    assert outcomes.count(True) == 1
    assert len(backend.list_keys(root.id)) == 1


def test_key_rebind_compare_and_swap_is_atomic_under_concurrent_writers() -> None:
    """Only one writer can replace a binding from the same expected block."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    initial = memory.put(type="value", value="initial")
    replacements = [memory.put(type="value", value=index) for index in range(12)]
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress((), "winner")
    collection.bind(address, initial)

    def try_rebind(index: int) -> bool:
        try:
            collection.rebind(address, replacements[index], expected=initial)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(try_rebind, range(len(replacements))))

    assert outcomes.count(True) == 1
    current = collection.get(address)
    assert current is not None
    assert current.id in {block.id for block in replacements}


def test_keyed_collection_requires_the_optional_backend_capability() -> None:
    """A backend without keyed operations fails clearly at construction."""

    memory = Memory()
    root = memory.put(type="collection", value="preferences")

    with pytest.raises(UnsupportedOperation):
        KeyedCollection(memory, object(), collection=root)  # type: ignore[arg-type]


def test_memory_and_handle_implement_keyed_access() -> None:
    """Both public access objects can resolve blocks for a keyed collection."""

    memory = Memory()
    root = memory.put(type="root", value="root")

    assert isinstance(memory, KeyedAccess)
    assert isinstance(memory.get_handle(root=root), KeyedAccess)
    assert isinstance(memory.backend, KeyedStore)
