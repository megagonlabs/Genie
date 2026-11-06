"""Ask a facade which block it is bound to, and treat that block as memory.

- **One container block:** a container-based facade holds a reference to one
  ordinary memory block. This block is a tree root, a collection, or a log.
- **Id and block:** each facade gives the id as a plain attribute. It gives the
  block as a property that reads storage again. So a read of the id does not
  use storage and the id does not change. The property always shows the
  current value of the block.
- **Ordinary memory:** the application can rename the container, search for it,
  and protect it with a policy, as with any other block. After a delete of the
  container, the facade reports a missing container. It does not return old
  results.
- **Membership is not ownership:** membership records refer to blocks by id and
  do not change ownership. A block that a facade binds keeps its parent. Search
  scoped to a container follows parents, not membership.
- **Link-based facades have no container block:** `LineageDAG` binds to one
  relation. `PropertyGraph` binds to no relation, and its calls take an
  optional `relation` filter.

Run from the repository root:

    python examples/advanced/facade_containers.py
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
    """Read the container of each facade, then rename one and delete it."""

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

    # Each container-based facade reports the block that it binds to.
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
    # `VersionedRecords` reuses a keyed collection, so it reports the same
    # collection block.
    assert versions.collection_id == versions_root.id
    assert versions.collection == versions_root

    # The container is ordinary memory, so a rename is a normal write. The
    # property shows the new value, and the id does not change.
    renamed = memory.set(catalog_root, "ranker catalog")

    print(f"catalog collection after renaming: {catalog.collection.value}")

    assert catalog.collection_id == catalog_root.id
    assert catalog.collection.value == "ranker catalog"
    assert catalog.collection.version == renamed.version
    # Search finds a container, as it finds any other block.
    assert [result.block.id for result in memory.search("ranker catalog")] == [catalog_root.id]

    # A keyed read returns the block (`get`) or the block with its binding
    # (`get_item`). The binding records the address. The block does not.
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
    # The bind recorded membership and did not change ownership. So the ranker
    # is still a child of the workspace. Search scopes follow parents, so a
    # search from the catalog root does not find the ranker.
    assert list(memory.search("candidate ranker", root=catalog_root, scope="subtree")) == []
    assert [
        result.block.id
        for result in memory.search("candidate ranker", root=workspace, scope="subtree")
    ] == [ranker.id]
    # For an unbound address, both forms return `None` and raise no error.
    assert catalog.get(MemoryAddress(("serving",), "missing")) is None
    assert catalog.get_item(MemoryAddress(("serving",), "missing")) is None

    # After a delete of the container, the facade reports a missing container
    # with `MemoryNotFound`. It does not act like an empty container.
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

    # The id is a plain attribute, so a read of the id still works and does
    # not use storage.
    assert catalog.collection_id == catalog_root.id
    # The delete has no effect on the other facades. Each facade binds to its
    # own block.
    assert events.log == events_root
    assert timeline.collection == timeline_root
    print("facades bound to other blocks are unaffected")


if __name__ == "__main__":
    main()
