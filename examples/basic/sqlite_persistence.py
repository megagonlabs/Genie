"""Store memory blocks and links on disk with the bundled SQLite backend.

Run from the repository root:

    python examples/basic/sqlite_persistence.py
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import Memory, SQLiteBackend


def main() -> None:
    """Write a SQLite database, close it, and read it through a new backend."""

    print("== SQLite persistence ==")
    with TemporaryDirectory(prefix="genie-example-") as directory:
        database_path = Path(directory) / "memory.db"

        with SQLiteBackend(database_path) as backend:
            memory = Memory(backend=backend, principal="app:demo")
            project = memory.put(type="project", value="Persistent proposal")
            note = memory.put(
                parent=project,
                type="note",
                value={
                    "text": "Keep the persistent example concise.",
                    "status": "draft",
                },
            )
            link = memory.add_link(project, note, "documents")
            project_id = project.id
            note_id = note.id

        # A new backend instance opens the same database. Memory, policy,
        # codec, and structure APIs do not change when storage changes.
        with SQLiteBackend(database_path) as backend:
            reopened = Memory(backend=backend, principal="app:demo")
            stored_project = reopened.get(project_id)
            stored_note = reopened.get(note_id)
            results = reopened.search(
                "persistent",
                root=project_id,
                scope="subtree",
            )
            links = reopened.list_links(project_id, relation="documents")

            assert stored_project is not None
            assert stored_note is not None
            assert [result.block.id for result in results] == [project_id, note_id]
            assert links == [link]

            updated = reopened.update(
                note_id,
                value={"status": "verified"},
                metadata={"storage": "sqlite"},
            )
            assert updated.value == {
                "text": "Keep the persistent example concise.",
                "status": "verified",
            }
            assert updated.metadata == {"storage": "sqlite"}

        print(f"reopened project: {stored_project.value}")
        print(f"reopened note: {stored_note.value}")
        print(f"persisted link: {links[0].relation}")
        print(f"updated note version: {updated.version}")


if __name__ == "__main__":
    main()
