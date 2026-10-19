"""Browse a catalog of versioned records, then prune one to retire it.

- **Listing:** `VersionedRecords.identities` lists the records that a catalog
  registered, by exact namespace or recursively. It does not read revision
  history.
- **Retirement is an ownership decision, not a catalog decision:**
  `delete_subtree` removes an identity block and the revisions that it owns.
  Then the catalog reports that record as absent. It does not raise an error.

Run from the repository root:

    python examples/advanced/versioned_catalog.py
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

from genie import AccessDenied, InMemoryBackend, Memory
from genie.structures import MemoryAddress, OwnershipTree, VersionedRecords

PRODUCTION = MemoryAddress(("rankers",), "production")
CANARY = MemoryAddress(("rankers",), "canary")
NIGHTLY = MemoryAddress(("rankers", "experimental"), "nightly")


def main() -> None:
    """List registered records, then retire one and list them again."""

    print("== Versioned catalog ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="app:registry")
    workspace = memory.put(type="workspace", value="model registry")
    handle = memory.get_handle(root=workspace, allow_up=True)
    tree = OwnershipTree(handle, root=workspace)

    catalog_root = tree.create_child(type="collection", value="registered rankers")
    catalog = VersionedRecords(handle, backend, collection=catalog_root)

    # Each record has one stable identity block that owns its revisions. So a
    # prune of the identity block also prunes its history.
    production = tree.create_child(type="identity", value="production ranker")
    canary = tree.create_child(type="identity", value="canary ranker")
    nightly = tree.create_child(type="identity", value="nightly ranker")

    catalog.register(PRODUCTION, production)
    catalog.register(CANARY, canary)
    catalog.register(NIGHTLY, nightly)
    catalog.append(
        PRODUCTION,
        tree.create_child(parent=production, type="revision", value={"recall": 0.78}),
    )
    catalog.append(
        PRODUCTION,
        tree.create_child(parent=production, type="revision", value={"recall": 0.84}),
    )
    catalog.append(
        CANARY,
        tree.create_child(parent=canary, type="revision", value={"recall": 0.80}),
    )

    # An exact namespace lists only the records registered directly under it.
    direct = catalog.identities(namespace=("rankers",))
    nested = catalog.identities(namespace=("rankers",), recursive=True)
    everything = catalog.identities()

    print(f"registered directly under 'rankers': {[item.binding.address.key for item in direct]}")
    print(f"registered below 'rankers': {[item.binding.address.key for item in nested]}")

    # A listing sorts by full address: first by namespace, then by key. So a
    # nested namespace comes after the entries of its parent namespace.
    assert [item.binding.address.key for item in direct] == ["canary", "production"]
    assert [item.binding.address for item in nested] == [CANARY, PRODUCTION, NIGHTLY]
    assert len(everything) == 3
    # `identities` does not read revision history.
    assert [item.block.id for item in direct] == [canary.id, production.id]

    # Each record has its own history, and revision numbers start at 1.
    current = catalog.current(PRODUCTION)
    history = catalog.history(PRODUCTION)
    assert current is not None

    print(f"production revisions: {[item.entry.revision for item in history]}")
    print(f"current production revision: {current.entry.revision} -> {current.block.value}")

    assert catalog.identity(PRODUCTION) == production
    assert [item.entry.revision for item in history] == [1, 2]
    assert current.entry.revision == 2
    assert current.block.value == {"recall": 0.84}
    # A registered record with no appended revision has no current revision.
    assert catalog.current(NIGHTLY) is None

    # The retirement of the canary is an ownership decision. `delete_subtree`
    # requires a target, so a delete of the whole tree is always explicit.
    canary_revision = catalog.current(CANARY)
    assert canary_revision is not None
    tree.delete_subtree(canary)

    print(f"records left after retiring the canary: {len(catalog.identities())}")

    assert memory.get(canary) is None
    assert memory.get(canary_revision.block) is None
    # The catalog lists only visible records, so the retired record is not in
    # the list.
    assert [item.binding.address for item in catalog.identities()] == [PRODUCTION, NIGHTLY]
    # A read of that exact address reports absence and does not raise an error.
    assert catalog.identity(CANARY) is None
    assert catalog.current(CANARY) is None
    assert catalog.history(CANARY) == []
    print("the retired record reports absence rather than an error")

    # The facade root is a boundary. The tree refuses to prune a block outside
    # its root, with `AccessDenied`.
    outside = memory.put(type="workspace", value="another registry")
    try:
        tree.delete_subtree(outside)
    except AccessDenied:
        print("pruning a block outside the tree: blocked as expected")
    else:
        raise AssertionError("An ownership tree must not delete outside its root.")

    assert memory.get(outside) is not None
    assert catalog.identity(PRODUCTION) == production


if __name__ == "__main__":
    main()
