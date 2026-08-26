"""Rewrite and observe every write a memory system performs.

Write hooks come in two shapes. A `before_*` callback that returns a value is a
transform: what it returns is what gets stored, and raising from it refuses the
operation. An `after_*` callback returns `None` and can only observe, which is
why the paired callbacks hand it both versions of what changed.

Link transforms may only adjust metadata. Rewriting an endpoint or the relation
is rejected, so a hook cannot quietly redirect a link.

Run from the repository root:

    conda run -n "genie" python examples/basic/write_hooks.py
"""

from __future__ import annotations

from dataclasses import replace

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    HookContext,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryHook,
    MemoryLink,
    Traversal,
)

PROTECTED = "retention-hold"


class Stamper(MemoryHook):
    """Transforming hook that annotates everything written through it.

    Attributes:
        proposed_updates: Current and proposed values seen by `before_update`.
    """

    def __init__(self) -> None:
        """Create an empty stamper."""

        self.proposed_updates: list[tuple[int, str]] = []

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Return the block that should actually be stored."""

        return block.with_updates(metadata={**block.metadata, "audited": True})

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Return the update that should actually be applied.

        The stored block and the proposal are both available, so a transform
        can carry values forward instead of guessing them.
        """

        self.proposed_updates.append((current.version, str(proposed.value)))
        edits = int(current.metadata.get("edits", 0)) + 1
        return proposed.with_updates(metadata={**proposed.metadata, "edits": edits})

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Return the link that should actually be stored.

        `replace` keeps every other field, including `created_at`, which the
        invariant check treats as identity rather than annotation.
        """

        return replace(link, metadata={**link.metadata, "stamped": True})

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Return the link update that should actually be applied."""

        return replace(proposed, metadata={**proposed.metadata, "restamped": True})


class Journal(MemoryHook):
    """Observing hook that records what each write actually did.

    Attributes:
        reads: Requested id and whether the block was found.
        stored: Ids of blocks written.
        updates: Version before and after each update.
        deletes: Types of blocks reported before and after deletion.
        links: Relation and metadata of each link event.
        traversals: Phase and number of results attached when the event fired.
    """

    def __init__(self) -> None:
        """Create an empty journal."""

        self.reads: list[tuple[str, bool]] = []
        self.stored: list[str] = []
        self.updates: list[tuple[int, int]] = []
        self.deletes: list[tuple[str, str]] = []
        self.links: list[tuple[str, dict[str, object]]] = []
        self.traversals: list[tuple[str, int]] = []
        self._pending_read = ""

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Record the id a read asked for, before it is known to exist."""

        self._pending_read = block_id

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Record whether the read found anything."""

        self.reads.append((self._pending_read, block is not None))

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a stored block."""

        self.stored.append(block.id)

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Record both versions of an updated block."""

        self.updates.append((previous.version, stored.version))

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a block that is about to be deleted."""

        self.deletes.append(("before", block.type))

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a block that was deleted. A recursive delete reports each."""

        self.deletes.append(("after", block.type))

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Record a stored link."""

        self.links.append((link.relation, dict(link.metadata)))

    def after_update_link(
        self,
        context: HookContext,
        previous: MemoryLink,
        stored: MemoryLink,
    ) -> None:
        """Record both versions of an updated link."""

        self.links.append((stored.relation, dict(stored.metadata)))

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record a traversal before its results are known."""

        self.traversals.append(("before", len(traversal.blocks)))

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record a traversal once its results are attached."""

        self.traversals.append(("after", len(traversal.blocks)))


class RetentionGuard(MemoryHook):
    """Hook that refuses to delete blocks under a retention hold."""

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Refuse deletion of a held block.

        Raises:
            PermissionError: If the block carries a retention hold.
        """

        if block.metadata.get(PROTECTED):
            raise PermissionError(f"Block {block.id!r} is under a retention hold.")


class Redirector(MemoryHook):
    """Hook that tries to repoint a link, which Genie rejects."""

    def __init__(self, target_id: str) -> None:
        """Create a redirector.

        Args:
            target_id: Target this hook tries to substitute.
        """

        self.target_id = target_id

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Return a link pointing somewhere else."""

        return replace(link, target_id=self.target_id)


def main() -> None:
    """Exercise every write callback, transforming and observing."""

    print("== Write hooks ==")
    stamper = Stamper()
    journal = Journal()
    memory = Memory(
        backend=InMemoryBackend(),
        principal="app:writer",
        hooks=[stamper, journal, RetentionGuard()],
    )

    # `before_put` decides what is stored, so the stored block already carries
    # the annotation the caller never supplied.
    task = memory.put(type="task", value={"title": "Draft outline", "status": "open"})

    print(f"metadata added before storage: {task.metadata}")

    assert task.metadata == {"audited": True}
    assert journal.stored == [task.id]

    # Reads report the requested id first and the outcome afterwards, so a miss
    # is recorded rather than invisible.
    found = memory.get(task)
    missing = memory.get("block-that-was-never-stored")

    print(f"reads recorded: {[outcome for _, outcome in journal.reads]}")

    assert found is not None
    assert missing is None
    assert journal.reads == [(task.id, True), ("block-that-was-never-stored", False)]

    # `before_update` sees the stored block and the proposal together, so it can
    # carry a counter forward. `after_update` sees both resulting versions.
    updated = memory.update(task, value={"status": "review"})
    again = memory.update(updated, value={"status": "done"})

    print(f"edit counter maintained by the hook: {again.metadata['edits']}")
    print(f"versions reported by after_update: {journal.updates}")

    assert stamper.proposed_updates[0][0] == task.version
    assert updated.metadata["edits"] == 1
    assert again.metadata["edits"] == 2
    assert journal.updates == [(task.version, updated.version), (updated.version, again.version)]
    assert again.value == {"title": "Draft outline", "status": "done"}

    # Link transforms annotate, and the observing callback sees the result.
    note = memory.put(type="note", value="Outline reviewed")
    link = memory.add_link(task, note, "documents", metadata={"confidence": 0.9})
    relinked = memory.update_link(link, metadata={"confidence": 1.0})

    print(f"stored link metadata: {link.metadata}")
    print(f"updated link metadata: {relinked.metadata}")

    # `update_link` merges metadata into what the link already carried, so the
    # annotation from the original insert survives the update.
    assert link.metadata == {"confidence": 0.9, "stamped": True}
    assert relinked.metadata == {"confidence": 1.0, "stamped": True, "restamped": True}
    assert journal.links == [
        ("documents", {"confidence": 0.9, "stamped": True}),
        ("documents", {"confidence": 1.0, "stamped": True, "restamped": True}),
    ]

    # A traversal event fires before results exist and again once they do, so
    # the trail records how much one call disclosed.
    subtask = memory.put(parent=task, type="task", value={"title": "Collect sources"})
    children = memory.children(task)

    print(f"traversal recorded: {journal.traversals}")

    assert [block.id for block in children] == [subtask.id]
    assert journal.traversals == [("before", 0), ("after", 1)]

    # A before_* hook refuses an operation by raising, and nothing is written.
    held = memory.put(type="record", value="Audited transaction", metadata={PROTECTED: True})
    try:
        memory.delete(held)
    except PermissionError:
        print("deleting a block under retention hold: refused by the hook")
    else:
        raise AssertionError("A retention hold must prevent deletion.")

    assert memory.get(held) is not None
    assert ("after", "record") not in journal.deletes

    # A recursive delete reports every block it removes, parent and child.
    parent = memory.put(type="project", value="Retired workspace")
    memory.put(parent=parent, type="note", value="Superseded note")
    journal.deletes.clear()
    memory.delete(parent)

    print(f"deletion events for a two-block subtree: {journal.deletes}")

    assert memory.get(parent) is None
    assert journal.deletes.count(("after", "note")) == 1
    assert journal.deletes.count(("after", "project")) == 1

    # A link transform may adjust metadata but not topology. Repointing a link
    # is rejected instead of silently redirecting it.
    other = Memory(backend=InMemoryBackend(), principal="app:writer")
    first = other.put(type="node", value="first")
    second = other.put(type="node", value="second")
    third = other.put(type="node", value="third")
    other.hooks.register(Redirector(third.id))
    try:
        other.add_link(first, second, "points_to")
    except ValueError:
        print("a hook repointing a link: refused")
    else:
        raise AssertionError("A hook must not change link topology.")

    assert other.list_links(first) == []


if __name__ == "__main__":
    main()
