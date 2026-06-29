"""Pass scoped memory handles to agents and sub-agents.

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

from genie import AccessDenied, Memory, MemoryBlock, MemoryScope


@dataclass
class Agent:
    name: str
    memory: MemoryScope

    def remember(self, text: str) -> MemoryBlock:
        return self.memory.put(type="note", value=f"{self.name}: {text}")


def main() -> None:
    memory = Memory(principal="system")
    project = memory.put(type="project", value="Project workspace")
    task = memory.put(parent=project, type="task", value="Write summary")
    sibling = memory.put(parent=project, type="task", value="Unrelated task")

    planner = Agent("planner", memory.scope(root=project, principal="agent:planner"))
    writer = Agent("writer", planner.memory.scope(root=task))
    note = writer.remember("use concise bullets")

    assert writer.memory.get(note) is not None
    assert [result.block.id for result in writer.memory.search("concise")] == [note.id]

    try:
        writer.memory.get(sibling)
    except AccessDenied as exc:
        print(f"scope blocked sibling read: {exc}")

    print(f"writer only sees task subtree rooted at {task.id}")


if __name__ == "__main__":
    main()
