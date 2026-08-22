"""Ask a facade which block it is bound to, and treat that block as memory.

Container-based facades hold a reference to one ordinary memory block: a tree
root, a collection, or a log. Each exposes the id as a plain attribute and the
block itself as a property that re-reads storage, so the id stays cheap and
stable while the property always reflects the block's current value.

Because the container is ordinary memory, it can be renamed, searched, and
policy-protected like anything else, and deleting it makes the facade report a
missing container rather than returning stale results. Link-based facades such
as `PropertyGraph` and `LineageDAG` bind to a relation instead, so they have no
container block.

Run from the repository root:

    conda run -n "genie" python examples/advanced/facade_containers.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory, MemoryNotFound
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    OwnershipTree,
    TemporalCollection,
    VersionedRecords,
)

ACTIVE = MemoryAddress(("serving",), "active_model")


def main() -> None:
    """Read every facade's container, then rename and delete one."""

    print("== Facade containers ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="app:registry")
    workspace = memory.put(type="workspace", value="model workspace")
    tree = OwnershipTree(memory, root=workspace)

    catalog_root = tree.create_child(type="collection", value="model catalog")
    events_root = tree.create_child(type="log", value="training events")
    timeline_root = tree.create_child(type="collection", value="deployment timeline")
    versions_root = tree.create_child(type="collection", value="model versions")

    catalog = KeyedCollection(memory, backend, collection=catalog_root)
    events = EventLog(memory, backend, log=events_root)
    timeline = TemporalCollection(memory, backend, collection=timeline_root)
    versions = VersionedRecords(memory, backend, collection=versions_root)

    # Every container-based facade reports the block it is bound to.
    print(f"tree root: {tree.root.value}")
    print(f"catalog collection: {catalog.collection.value}")
    print(f"log: {events.log.value}")

    assert tree.root_id == workspace.id
    assert tree.root == workspace
    assert catalog.collection_id == catalog_root.id
    assert catalog.collection == catalog_root
    assert events.log_id == events_root.id
    assert events.log == events_root
    assert timeline.collection_id == timeline_root.id
    assert timeline.collection == timeline_root
    # Versioned records reuse a keyed collection, so they report the same one.
    assert versions.collection_id == versions_root.id
    assert versions.collection == versions_root

    # The container is ordinary memory. Renaming it is a normal write, and the
    # property reflects it while the id stays the same.
    renamed = memory.set(catalog_root, "ranker catalog")

    print(f"catalog collection after renaming: {catalog.collection.value}")

    assert catalog.collection_id == catalog_root.id
    assert catalog.collection.value == "ranker catalog"
    assert catalog.collection.version == renamed.version
    # Containers are searchable like any other block.
    assert [result.block.id for result in memory.search("ranker catalog")] == [catalog_root.id]

    # A keyed read can return the block or the binding that reached it. The
    # binding names the address, which the block itself does not carry.
    ranker = tree.create_child(type="model", value="candidate ranker")
    catalog.bind(ACTIVE, ranker)
    block = catalog.get(ACTIVE)
    item = catalog.get_item(ACTIVE)

    assert item is not None
    print(f"bound at {item.binding.address.namespace + (item.binding.address.key,)}")

    assert block == ranker
    assert item.block == ranker
    assert item.binding.address == ACTIVE
    assert item.binding.collection_id == catalog_root.id
    # An unbound address is absent rather than an error, in both forms.
    assert catalog.get(MemoryAddress(("serving",), "missing")) is None
    assert catalog.get_item(MemoryAddress(("serving",), "missing")) is None

    # Deleting the container makes the facade report a missing container rather
    # than silently behaving like an empty one.
    memory.delete(catalog_root)

    try:
        _ = catalog.collection
    except MemoryNotFound:
        print("reading the collection after deleting it: MemoryNotFound")
    else:
        raise AssertionError("A deleted container must not resolve.")
    try:
        catalog.get(ACTIVE)
    except MemoryNotFound:
        print("keyed read after deleting the collection: MemoryNotFound")
    else:
        raise AssertionError("A deleted container must not serve reads.")

    # The id is a plain attribute, so it still answers without storage.
    assert catalog.collection_id == catalog_root.id
    # The other facades are unaffected; each binds to its own block.
    assert events.log == events_root
    assert timeline.collection == timeline_root
    print("facades bound to other blocks are unaffected")


if __name__ == "__main__":
    main()
