"""Pass scoped memory handles to agents and sub-agents.

Most agents should receive bounded scopes created with `memory.scope(root=...)`.
The example also shows `memory.unbounded_scope(...)` for a trusted supervisor
handle, where unbounded access is deliberate and visible at the call site.

Run from the repository root:

    conda run -n "genie" python examples/scoped_agents.py
"""

from __future__ import annotations

from dataclasses import dataclass

if __package__ in {None, ""}:
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import AccessDenied, InMemoryBackend, Memory, MemoryBlock, MemoryScope


@dataclass
class Agent:
    """Tiny example agent that receives only a scoped memory handle.

    Attributes:
        name: Human-readable agent name used in written notes.
        memory: Scoped memory handle passed in by the application.
    """

    name: str
    memory: MemoryScope

    def remember(self, text: str) -> MemoryBlock:
        """Write a note into the agent's scoped memory.

        Args:
            text: Note text.

        Returns:
            Created memory block.
        """

        return self.memory.put(type="note", value=f"{self.name}: {text}")


def main() -> None:
    """Run a focused scoped-agent walkthrough."""

    memory = Memory(backend=InMemoryBackend(), principal="system")
    project = memory.put(type="project", value="Project workspace")
    task = memory.put(parent=project, type="task", value="Write summary")
    sibling = memory.put(parent=project, type="task", value="Unrelated task")

    planner = Agent("planner", memory.scope(root=project, principal="agent:planner"))
    writer = Agent("writer", planner.memory.scope(root=task))
    supervisor_memory = memory.unbounded_scope(
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

    try:
        writer.memory.get(sibling)
    except AccessDenied as exc:
        print(f"scope blocked sibling read: {exc}")
    try:
        supervisor_memory.search("concise")
    except AccessDenied as exc:
        print(f"unbounded scope needs an explicit root for subtree search: {exc}")

    print(f"writer only sees task subtree rooted at {task.id}")


if __name__ == "__main__":
    main()
