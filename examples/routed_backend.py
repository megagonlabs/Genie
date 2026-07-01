"""Route memory blocks across multiple backend stores.

Run from the repository root:

    conda run -n "genie" python examples/routed_backend.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, InMemoryLinkStore, Memory, MemoryBlock, RoutedBackend


def route_by_region(block: MemoryBlock) -> str | None:
    """Choose a backend route from block metadata.

    Args:
        block: Block being stored.

    Returns:
        Route key when the block declares a region, otherwise `None`.
    """

    region = block.metadata.get("region")
    return region if isinstance(region, str) else None


def main() -> None:
    """Run a focused routed backend walkthrough."""

    us_store = InMemoryBackend()
    eu_store = InMemoryBackend()
    backend = RoutedBackend(
        {
            "us": us_store,
            "eu": eu_store,
        },
        route_selector=route_by_region,
        default_route="us",
        link_store=InMemoryLinkStore(),
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

    global_results = memory.search("launch", scope="global")
    eu_results = memory.search("data residency", root=eu_project, scope="subtree")
    mount = memory.add_mount(us_project, eu_project, metadata={"reason": "shared rollout context"})
    mounted_root = memory.resolve_mount(mount)

    print(f"US project route: {backend.route_for_block(us_project.id)}")
    print(f"EU project route: {backend.route_for_block(eu_project.id)}")
    print(f"US note inherited route: {backend.route_for_block(us_note.id)}")
    print(f"EU subtree search: {eu_results[0].block.value}")
    print(f"Mounted route target: {mounted_root.value if mounted_root else None}")

    assert backend.route_for_block(us_project.id) == "us"
    assert backend.route_for_block(us_note.id) == "us"
    assert backend.route_for_block(eu_project.id) == "eu"
    assert backend.route_for_block(eu_note.id) == "eu"
    assert us_store.get_block(us_project.id) is not None
    assert us_store.get_block(eu_project.id) is None
    assert eu_store.get_block(eu_project.id) is not None
    assert eu_store.get_block(us_project.id) is None
    assert {result.block.id for result in global_results} >= {us_project.id, eu_note.id}
    assert [result.block.id for result in eu_results] == [eu_note.id]
    assert mounted_root is not None
    assert mounted_root.id == eu_project.id

    memory.delete(us_project)

    assert memory.get(us_project) is None
    assert memory.get(us_note) is None
    assert memory.get(eu_project) is not None
    assert memory.get(eu_note) is not None


if __name__ == "__main__":
    main()
