"""Represent cross-memory relationships with typed links.

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
    """Run a focused link creation and linked search walkthrough."""

    memory = Memory(backend=InMemoryBackend())
    requirement = memory.put(type="requirement", value="Mention deployment tradeoffs.")
    outline = memory.put(type="outline", value="Sections: context, tradeoffs, next steps.")
    note = memory.put(type="note", value="Deployment note is linked from requirement.")

    informs = memory.add_link(requirement, outline, "informs", metadata={"weight": 0.9})
    memory.add_link(note, requirement, "mentions")

    outgoing = memory.list_links(requirement)
    incoming = memory.list_links(requirement, direction="in")
    related = memory.search("sections", root=requirement, scope="links")

    print(f"outgoing relation: {outgoing[0].relation}")
    print(f"incoming relation: {incoming[0].relation}")
    print(f"linked search result: {related[0].block.value}")

    assert outgoing == [informs]
    assert [result.block.id for result in related] == [outline.id]


if __name__ == "__main__":
    main()
