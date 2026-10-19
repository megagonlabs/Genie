"""Rewrite and observe every write that a memory system does.

Write hooks have 2 shapes:

- **Transform:** a `before_*` callback that returns a value is a transform.
  `Memory` stores the value that the callback returns.
- **Refusal:** an exception from a `before_*` callback refuses the operation.
- **Observer:** an `after_*` callback returns `None` and can only observe. Thus
  the paired update callbacks give it both versions of the changed item.
- **Link limits:** a link transform can change only metadata. `Memory` refuses
  a changed endpoint or relation with `ValueError`, so a hook cannot secretly
  redirect a link.

Run from the repository root:

    python examples/basic/write_hooks.py
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
    """Transform hook that adds metadata to each block and link that `Memory` writes.

    Attributes:
        proposed_updates: Current version and proposed value of each update that
            `before_update` receives.
    """

    def __init__(self) -> None:
        """Create an empty stamper."""

        self.proposed_updates: list[tuple[int, str]] = []

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Add the `audited` flag to the block metadata, and return the block to store."""

        return block.with_updates(metadata={**block.metadata, "audited": True})

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Add 1 to the `edits` counter, and return the update to apply.

        The hook receives both the stored block and the proposal. Thus a
        transform can read old values from the stored block and does not guess
        them.
        """

        self.proposed_updates.append((current.version, str(proposed.value)))
        edits = int(current.metadata.get("edits", 0)) + 1
        return proposed.with_updates(metadata={**proposed.metadata, "edits": edits})

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Add the `stamped` flag to the link metadata, and return the link to store.

        `replace` keeps every other field, for example `created_at`. The
        invariant check treats `created_at` as identity, not as an annotation.
        """

        return replace(link, metadata={**link.metadata, "stamped": True})

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Add the `restamped` flag to the link metadata, and return the link update to apply."""

        return replace(proposed, metadata={**proposed.metadata, "restamped": True})


class Journal(MemoryHook):
    """Observer hook that records the result of each read, write, and traversal.

    Attributes:
        reads: Requested id, and `True` if the read found the block.
        stored: Ids of the stored blocks.
        updates: Version before and after each update.
        deletes: Phase (`"before"` or `"after"`) and block type of each delete
            event.
        links: Relation and metadata of each link event.
        traversals: Phase, and the number of result blocks that the traversal
            had at that event.
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
        """Record the id that a read requests, before `Memory` knows if the block exists."""

        self._pending_read = block_id

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Record if the read found a block."""

        self.reads.append((self._pending_read, block is not None))

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record the id of a stored block."""

        self.stored.append(block.id)

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Record the old and new version numbers of an updated block."""

        self.updates.append((previous.version, stored.version))

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record the type of a block before `Memory` deletes it."""

        self.deletes.append(("before", block.type))

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record the type of a deleted block. A recursive delete reports each deleted block."""

        self.deletes.append(("after", block.type))

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Record the relation and metadata of a stored link."""

        self.links.append((link.relation, dict(link.metadata)))

    def after_update_link(
        self,
        context: HookContext,
        previous: MemoryLink,
        stored: MemoryLink,
    ) -> None:
        """Record the relation and metadata of the new version of an updated link."""

        self.links.append((stored.relation, dict(stored.metadata)))

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record the result count of a traversal before the read runs."""

        self.traversals.append(("before", len(traversal.blocks)))

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record the result count of a traversal after the read attaches its results."""

        self.traversals.append(("after", len(traversal.blocks)))


class RetentionGuard(MemoryHook):
    """Hook that refuses the delete of a block under a retention hold."""

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Refuse the delete of a held block.

        Raises:
            PermissionError: If the block metadata has a retention hold.
        """

        if block.metadata.get(PROTECTED):
            raise PermissionError(f"Block {block.id!r} is under a retention hold.")


class Redirector(MemoryHook):
    """Hook that tries to change the target of a link. `Memory` refuses this change."""

    def __init__(self, target_id: str) -> None:
        """Create a redirector.

        Args:
            target_id: Id of the target that this hook tries to put in the link.
        """

        self.target_id = target_id

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Return a copy of the link with a different target."""

        return replace(link, target_id=self.target_id)


def main() -> None:
    """Run each write callback of the transform hooks and the observer hooks."""

    print("== Write hooks ==")
    stamper = Stamper()
    journal = Journal()
    memory = Memory(
        backend=InMemoryBackend(),
        principal="app:writer",
        hooks=[stamper, journal, RetentionGuard()],
    )

    # `before_put` decides what `Memory` stores. Thus the stored block already has
    # the annotation, although the caller did not supply it.
    task = memory.put(type="task", value={"title": "Draft outline", "status": "open"})

    print(f"metadata added before storage: {task.metadata}")

    assert task.metadata == {"audited": True}
    assert journal.stored == [task.id]

    # A read reports the requested id first and the result after it. Thus the
    # journal also records a miss, and the miss does not stay invisible.
    found = memory.get(task)
    missing = memory.get("block-that-was-never-stored")

    print(f"reads recorded: {[outcome for _, outcome in journal.reads]}")

    assert found is not None
    assert missing is None
    assert journal.reads == [(task.id, True), ("block-that-was-never-stored", False)]

    # `before_update` receives the stored block and the proposal together. Thus it
    # can increase a counter from the stored value. `after_update` receives the
    # old and the new version.
    updated = memory.update(task, value={"status": "review"})
    again = memory.update(updated, value={"status": "done"})

    print(f"edit counter maintained by the hook: {again.metadata['edits']}")
    print(f"versions reported by after_update: {journal.updates}")

    assert stamper.proposed_updates[0][0] == task.version
    assert updated.metadata["edits"] == 1
    assert again.metadata["edits"] == 2
    assert journal.updates == [(task.version, updated.version), (updated.version, again.version)]
    assert again.value == {"title": "Draft outline", "status": "done"}

    # Link transforms add metadata, and the observer callback receives the result.
    note = memory.put(type="note", value="Outline reviewed")
    link = memory.add_link(task, note, "documents", metadata={"confidence": 0.9})
    relinked = memory.update_link(link, metadata={"confidence": 1.0})

    print(f"stored link metadata: {link.metadata}")
    print(f"updated link metadata: {relinked.metadata}")

    # `update_link` merges the new metadata into the stored link metadata. Thus
    # the annotation from the first insert stays after the update.
    assert link.metadata == {"confidence": 0.9, "stamped": True}
    assert relinked.metadata == {"confidence": 1.0, "stamped": True, "restamped": True}
    assert journal.links == [
        ("documents", {"confidence": 0.9, "stamped": True}),
        ("documents", {"confidence": 1.0, "stamped": True, "restamped": True}),
    ]

    # A traversal event occurs before the read has results, and again after the
    # read has them. Thus the trail records how much one call disclosed.
    subtask = memory.put(parent=task, type="task", value={"title": "Collect sources"})
    children = memory.children(task)

    print(f"traversal recorded: {journal.traversals}")

    assert [block.id for block in children] == [subtask.id]
    assert journal.traversals == [("before", 0), ("after", 1)]

    # A `before_*` hook refuses an operation with an exception, and `Memory` writes nothing.
    held = memory.put(type="record", value="Audited transaction", metadata={PROTECTED: True})
    try:
        memory.delete(held)
    except PermissionError:
        print("deleting a block under retention hold: refused by the hook")
    else:
        raise AssertionError("A retention hold must prevent deletion.")

    assert memory.get(held) is not None
    assert ("after", "record") not in journal.deletes

    # A recursive delete reports each block that it removes, both parent and child.
    parent = memory.put(type="project", value="Retired workspace")
    memory.put(parent=parent, type="note", value="Superseded note")
    journal.deletes.clear()
    memory.delete(parent)

    print(f"deletion events for a two-block subtree: {journal.deletes}")

    assert memory.get(parent) is None
    assert journal.deletes.count(("after", "note")) == 1
    assert journal.deletes.count(("after", "project")) == 1

    # A link transform can change metadata but not topology. `Memory` refuses a
    # changed link target with `ValueError` and does not silently redirect the link.
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
