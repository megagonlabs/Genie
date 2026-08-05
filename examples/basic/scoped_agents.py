"""Pass bounded memory handles to agents and sub-agents.

Most agents should receive bounded handles created with `memory.get_handle(root=...)`.
The example also shows `memory.get_unbounded_handle(...)` for a trusted supervisor
handle, where unbounded access is deliberate and visible at the call site.

Run from the repository root:

    conda run -n "genie" python examples/basic/scoped_agents.py
"""

from __future__ import annotations

from dataclasses import dataclass

if __package__ in {None, ""}:
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import AccessDenied, InMemoryBackend, Memory, MemoryBlock, MemoryHandle, MemoryPolicy


@dataclass
class Agent:
    """Tiny example agent that receives only a bounded memory handle.

    Attributes:
        name: Human-readable agent name used in written notes.
        memory: Bounded memory handle passed in by the application.
    """

    name: str
    memory: MemoryHandle

    def remember(self, text: str) -> MemoryBlock:
        """Write a note through the agent's memory handle.

        Args:
            text: Note text.

        Returns:
            Created memory block.
        """

        return self.memory.put(type="note", value=f"{self.name}: {text}")


def main() -> None:
    """Run a focused memory-handle walkthrough."""

    print("== Scoped agent handles ==")
    # This example focuses on handle boundaries, so blocks are explicitly
    # shared. Production apps should choose a policy that matches their users.
    memory = Memory(
        backend=InMemoryBackend(),
        principal="system",
        default_policy=MemoryPolicy.public(),
    )
    project = memory.put(type="project", value="Project workspace")
    task = memory.put(parent=project, type="task", value="Write summary")
    sibling = memory.put(parent=project, type="task", value="Unrelated task")

    planner = Agent("planner", memory.get_handle(root=project, principal="agent:planner"))
    writer = Agent("writer", planner.memory.get_handle(root=task))
    supervisor_memory = memory.get_unbounded_handle(
        principal="agent:supervisor",
        allow_global=True,
    )
    note = writer.remember("use concise bullets")

    assert writer.memory.get(note) is not None
    assert [result.block.id for result in writer.memory.search("concise")] == [note.id]
    assert [result.block.id for result in supervisor_memory.search("concise", root=project)] == [
        note.id
    ]
    global_matches = supervisor_memory.search("Unrelated", scope="global")
    assert [result.block.id for result in global_matches] == [sibling.id]
    print(f"writer remembered: {note.value}")
    print(f"writer scope root: {task.value}")

    try:
        writer.memory.get(sibling)
    except AccessDenied:
        print("sibling task read: blocked as expected")
    else:
        raise AssertionError("A bounded writer handle must not read a sibling task.")
    try:
        supervisor_memory.search("concise")
    except AccessDenied:
        print("unbounded subtree search without a root: blocked as expected")
    else:
        raise AssertionError("An unbounded subtree search must specify a root.")

    print(f"supervisor global search: {global_matches[0].block.value}")


if __name__ == "__main__":
    main()
