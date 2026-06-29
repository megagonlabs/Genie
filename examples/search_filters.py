"""Search memory with text, types, scopes, and metadata filters.

Run from the repository root:

    conda run -n "genie" python examples/search_filters.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import Memory


def main() -> None:
    """Run a focused search and filter walkthrough."""

    memory = Memory()
    project = memory.put(type="project", value="Proposal workspace")
    selected = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database agnostic.",
        metadata={"priority": 3, "owner": "team"},
    )
    memory.put(
        parent=project,
        type="requirement",
        value="Include optional implementation notes.",
        metadata={"priority": 1, "owner": "team"},
    )
    memory.put(type="note", value="Global note outside the project", metadata={"priority": 5})

    results = memory.search(
        "proposal",
        root=project,
        scope="subtree",
        types=["requirement"],
        filters={"metadata.priority": {">=": 2}, "owner": {"in": ["team"]}},
    )

    for result in results:
        print(f"{result.score:.0f} {result.block.type}: {result.block.value}")

    assert [result.block.id for result in results] == [selected.id]


if __name__ == "__main__":
    main()
