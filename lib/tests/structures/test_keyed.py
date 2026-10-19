"""Tests for namespaced keyed collections and their atomic backend capability."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import cast

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
    """A collection binds a namespaced address to a stored block, and an unbound address returns `None`."""

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
    """`items` lists bindings in a stable order for an exact namespace or, with `recursive`, a namespace prefix."""

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
    assert collection.items(limit=0) == []

    with pytest.raises(TypeError, match="recursive must be a boolean"):
        collection.items(recursive=cast(bool, 1))
    with pytest.raises(TypeError, match="limit must be an integer"):
        collection.items(limit=cast(int, 1.5))
    with pytest.raises(ValueError, match="limit must be greater than or equal to zero"):
        collection.items(limit=-1)


def test_keyed_collection_uses_atomic_compare_and_swap_updates() -> None:
    """`rebind` and `unbind` raise `MemoryConflict` if the address holds a block other than `expected`."""

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
    """An address is unique only inside one collection id, so 2 collections can bind it to different blocks."""

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
    """The facade reads more than one store page to return the full `limit` of visible items."""

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
    """In a scoped collection, `get` and `bind` raise `AccessDenied` for an outside block, and `items` omits it."""

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
    """A delete of a target block or of the collection block removes the bindings that depend on it."""

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


def test_keyed_collection_handles_a_stale_backend_binding_defensively() -> None:
    """If a backend keeps a stale binding, `get_item` raises `MemoryNotFound` and `items` omits the deleted target."""

    class StaleKeyBackend(InMemoryBackend):
        """Simulate an adapter that leaves a binding after target deletion."""

        def delete_key_bindings_for_block(self, block_id: str) -> None:
            """Keep the key bindings on purpose so that the test can check stale state."""

    backend = StaleKeyBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="collection", value="preferences")
    target = memory.put(type="preference", value="stale")
    collection = KeyedCollection(memory, backend, collection=root)
    address = MemoryAddress((), "stale")
    collection.bind(address, target)
    memory.delete(target)

    with pytest.raises(MemoryNotFound, match="references missing memory block"):
        collection.get_item(address)
    assert collection.items() == []


def test_key_binding_is_atomic_under_concurrent_writers() -> None:
    """Of many concurrent `bind` calls for one unused address, only 1 call succeeds."""

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
    """Of many concurrent `rebind` calls with the same `expected` block, only 1 call succeeds."""

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
    """The constructor raises `UnsupportedOperation` for a backend without keyed operations."""

    memory = Memory()
    root = memory.put(type="collection", value="preferences")
    store = memory.backend
    assert isinstance(store, KeyedStore)

    with pytest.raises(UnsupportedOperation):
        KeyedCollection(memory, object(), collection=root)  # type: ignore[arg-type]

    with pytest.raises(MemoryNotFound, match="collection block"):
        KeyedCollection(memory, store, collection="missing")

    collection = KeyedCollection(memory, store, collection=root)
    with pytest.raises(TypeError, match="address must be a MemoryAddress"):
        collection.get(cast(MemoryAddress, "bad-address"))
    with pytest.raises(MemoryNotFound, match="Memory block 'missing'"):
        collection.bind(MemoryAddress((), "missing"), "missing")


def test_memory_and_handle_implement_keyed_access() -> None:
    """`Memory` and `MemoryHandle` both satisfy `KeyedAccess`, and the reference backend satisfies `KeyedStore`."""

    memory = Memory()
    root = memory.put(type="root", value="root")

    assert isinstance(memory, KeyedAccess)
    assert isinstance(memory.get_handle(root=root), KeyedAccess)
    assert isinstance(memory.backend, KeyedStore)
