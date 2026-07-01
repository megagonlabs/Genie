"""Represent cross-memory relationships with typed links and mounts.

Run from the repository root:

    conda run -n "genie" python examples/links.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory


def main() -> None:
    """Run a focused link and symbolic mount walkthrough."""

    memory = Memory(backend=InMemoryBackend())
    requirement = memory.put(type="requirement", value="Mention deployment tradeoffs.")
    outline = memory.put(type="outline", value="Sections: context, tradeoffs, next steps.")
    note = memory.put(type="note", value="Deployment note is linked from requirement.")
    remote_root = memory.put(type="project", value="External deployment reference.")
    remote_note = memory.put(parent=remote_root, type="note", value="Remote rollout checklist.")

    informs = memory.add_link(requirement, outline, "informs", metadata={"weight": 0.9})
    memory.add_link(note, requirement, "mentions")
    mount = memory.add_mount(requirement, remote_root, metadata={"label": "external docs"})

    outgoing = memory.list_links(requirement)
    incoming = memory.list_links(requirement, direction="in")
    related = memory.search("sections", root=requirement, scope="links")
    mounts = memory.list_mounts(requirement)
    mounted_root = memory.resolve_mount(mount)
    subtree = memory.search("rollout", root=requirement, scope="subtree")

    print(f"outgoing relation: {outgoing[0].relation}")
    print(f"incoming relation: {incoming[0].relation}")
    print(f"linked search result: {related[0].block.value}")
    print(f"mount target: {mounted_root.value if mounted_root else None}")

    assert informs in outgoing
    assert [result.block.id for result in related] == [outline.id]
    assert mounts == [mount]
    assert mounted_root is not None
    assert mounted_root.id == remote_root.id
    assert subtree == []
    memory.delete(requirement)
    assert memory.get(remote_note) is not None


if __name__ == "__main__":
    main()
