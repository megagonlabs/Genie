"""Create and update typed links, and represent symbolic mounts.

Run from the repository root:

    conda run -n "genie" python examples/basic/links.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory


def main() -> None:
    """Run a focused link and symbolic mount walkthrough."""

    print("== Links and mounts ==")
    memory = Memory(backend=InMemoryBackend())
    requirement = memory.put(type="requirement", value="Mention deployment tradeoffs.")
    outline = memory.put(type="outline", value="Sections: context, tradeoffs, next steps.")
    note = memory.put(type="note", value="Deployment note is linked from requirement.")
    remote_root = memory.put(type="project", value="External deployment reference.")
    remote_note = memory.put(parent=remote_root, type="note", value="Remote rollout checklist.")

    informs = memory.add_link(requirement, outline, "informs", metadata={"weight": 0.9})
    memory.add_link(note, requirement, "mentions")
    mount = memory.add_mount(requirement, remote_root, metadata={"label": "external docs"})
    reviewed_informs = memory.update_link(informs, metadata={"status": "reviewed"})

    outgoing = memory.list_links(requirement)
    incoming = memory.list_links(requirement, direction="in")
    related = memory.search("sections", root=requirement, scope="links")
    mounts = memory.list_mounts(requirement)
    discovered_mounts = memory.discover_mounts(requirement)
    mounted_root = memory.resolve_mount(mount)
    subtree = memory.search("rollout", root=requirement, scope="subtree")

    print(f"outgoing relation from requirement: {outgoing[0].relation}")
    print(f"updated outgoing link metadata: {reviewed_informs.metadata}")
    print(f"incoming relation to requirement: {incoming[0].relation}")
    print(f"link-scoped search result: {related[0].block.value}")
    print(f"mounts discovered from requirement: {len(discovered_mounts)}")
    print(f"mount target: {mounted_root.value if mounted_root else None}")
    print("local subtree search includes the mounted target: no")

    assert reviewed_informs in outgoing
    assert reviewed_informs.id == informs.id
    assert reviewed_informs.metadata == {"weight": 0.9, "status": "reviewed"}
    assert informs.metadata == {"weight": 0.9}
    assert [result.block.id for result in related] == [outline.id]
    assert mounts == [mount]
    assert discovered_mounts == [mount]
    assert mounted_root is not None
    assert mounted_root.id == remote_root.id
    assert subtree == []
    memory.delete(requirement)
    assert memory.get(remote_note) is not None
    print("deleting the mount source deletes the mounted memory: no")


if __name__ == "__main__":
    main()
