"""Route memory blocks to more than one backend store.

Run from the repository root:

    python examples/basic/routed_backend.py
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

from genie import (
    InMemoryBackend,
    InMemoryLinkStore,
    LinkRelations,
    Memory,
    MemoryBlock,
    MemoryLink,
    RoutedBackend,
)


def route_by_region(block: MemoryBlock) -> str | None:
    """Choose a backend route from block metadata.

    Args:
        block: The block that the backend stores.

    Returns:
        The route key if the block metadata has a string `region`. Otherwise
        `None`.
    """

    region = block.metadata.get("region")
    return region if isinstance(region, str) else None


def main() -> None:
    """Run a short walkthrough of a routed backend."""

    print("== Routed storage and mounts ==")
    us_store = InMemoryBackend()
    eu_store = InMemoryBackend()
    link_store = InMemoryLinkStore()
    backend = RoutedBackend(
        {
            "us": us_store,
            "eu": eu_store,
        },
        route_selector=route_by_region,
        default_route="us",
        link_store=link_store,
    )
    memory = Memory(backend=backend)

    us_project = memory.put(type="project", value="US launch", metadata={"region": "us"})
    eu_project = memory.put(type="project", value="EU rollout", metadata={"region": "eu"})
    us_note = memory.put(
        parent=us_project,
        type="note",
        value="US users prefer concise release notes.",
    )
    eu_note = memory.put(
        parent=eu_project,
        type="note",
        value="EU launch requires data residency review.",
    )
    # `add_mount` returns the symbolic link as a `MemoryLink`. The link starts at
    # a source block and refers to the EU project. It does not create a local
    # child block. It does not make the EU tree part of the US subtree.
    eu_rollout_link = memory.add_mount(
        us_project,
        eu_project,
        metadata={"label": "EU rollout", "reason": "shared rollout context"},
    )

    global_results = memory.search("launch", scope="global")
    eu_results = memory.search("data residency", root=eu_project, scope="subtree")
    us_results = memory.search("data residency", root=us_project, scope="subtree")
    linked_results = memory.search("rollout", root=us_project, scope="links")
    discovered_mounts = memory.discover_mounts(us_project)
    mounted_root = memory.resolve_mount(eu_rollout_link)

    apac_store = InMemoryBackend()
    apac_memory = Memory(backend=apac_store)
    apac_project = apac_memory.put(type="project", value="APAC rollout")
    apac_note = apac_memory.put(parent=apac_project, type="note", value="APAC launch checklist")
    # There are 2 ways to write a mount link.
    # - **Public API:** `memory.add_mount()` validates both endpoints. Use it when
    #   the target route is already registered.
    # - **Adapter code:** for a pending mount from an external catalog or an
    #   import, adapter code can write the mount link in a shared `LinkStore`.
    #   The link metadata holds route information, not a live database connection.
    pending_apac_mount = link_store.add_link(
        MemoryLink(
            source_id=us_note.id,
            target_id=apac_project.id,
            relation=LinkRelations.MOUNT,
            metadata={"route": "apac", "backend": "in-memory"},
        )
    )
    pending_mounts = memory.discover_mounts(us_project)
    assert memory.resolve_mount(pending_apac_mount) is None

    backend.register_route("apac", apac_store)
    apac_mounted_root = memory.resolve_mount(pending_apac_mount)
    apac_results = memory.search("checklist", root=apac_project, scope="subtree")

    print(f"US project route: {backend.route_for_block(us_project.id)}")
    print(f"EU project route: {backend.route_for_block(eu_project.id)}")
    print(f"US note inherited route: {backend.route_for_block(us_note.id)}")
    print(f"mount link relation: {eu_rollout_link.relation}")
    print(f"mounts discovered before APAC registration: {len(pending_mounts)} (EU and APAC)")
    print("APAC mount before route registration: unresolved")
    print(f"EU subtree search result: {eu_results[0].block.value}")
    print(f"EU mount target: {mounted_root.value if mounted_root else None}")
    print(f"APAC project route after registration: {backend.route_for_block(apac_project.id)}")
    print(f"APAC mount after route registration: {apac_mounted_root.value if apac_mounted_root else None}")

    assert backend.route_for_block(us_project.id) == "us"
    assert backend.route_for_block(us_note.id) == "us"
    assert backend.route_for_block(eu_project.id) == "eu"
    assert backend.route_for_block(eu_note.id) == "eu"
    assert backend.route_for_block(apac_project.id) == "apac"
    assert us_store.get_block(us_project.id) is not None
    assert us_store.get_block(eu_project.id) is None
    assert eu_store.get_block(eu_project.id) is not None
    assert eu_store.get_block(us_project.id) is None
    assert apac_store.get_block(apac_project.id) is not None
    assert [block.id for block in memory.children(us_project)] == [us_note.id]
    assert {result.block.id for result in global_results} >= {us_project.id, eu_note.id}
    assert [result.block.id for result in eu_results] == [eu_note.id]
    assert us_results == []
    assert [result.block.id for result in linked_results] == [eu_project.id]
    assert [result.block.id for result in apac_results] == [apac_note.id]
    assert eu_rollout_link.relation == LinkRelations.MOUNT
    assert eu_rollout_link.source_id == us_project.id
    assert eu_rollout_link.target_id == eu_project.id
    assert memory.list_mounts(us_project) == [eu_rollout_link]
    assert discovered_mounts == [eu_rollout_link]
    assert pending_apac_mount in pending_mounts
    assert mounted_root is not None
    assert mounted_root.id == eu_project.id
    assert apac_mounted_root is not None
    assert apac_mounted_root.id == apac_project.id

    memory.delete(us_project)

    assert memory.get(us_project) is None
    assert memory.get(us_note) is None
    assert memory.get(eu_project) is not None
    assert memory.get(eu_note) is not None
    assert memory.get(apac_project) is not None
    assert memory.get(apac_note) is not None


if __name__ == "__main__":
    main()
