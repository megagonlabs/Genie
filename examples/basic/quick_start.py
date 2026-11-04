"""Quick start of the repository README: an application gives an LLM agent a bounded memory handle.

- **Same code as the README:** the Quick start in `README.md` shows this file from the `from genie import` line to the end.
  `lib/tests/test_examples.py` checks that the 2 stay the same.
- **Printed results:** the script prints its results, as the README does. It does not use assertions.

Run from the repository root:

    python examples/basic/quick_start.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import AccessDenied, InMemoryBackend, Memory, MemoryHandle, MemoryPolicy

# Scenario: an application runs an LLM agent that writes a proposal.
# The application owns the memory. The agent gets a handle to one project only.

# --- Application side (trusted code) ---
memory = Memory(backend=InMemoryBackend(), principal="app")

# New blocks use `owner_private` by default: only their creator can use them.
# `owner_or` also lets "agent:writer" use the blocks of the proposal project.
writer_access = MemoryPolicy.owner_or(principals={"agent:writer"})
proposal = memory.put(type="project", value={"name": "Write proposal"}, policy=writer_access)
requirement = memory.put(
    parent=proposal,
    type="requirement",
    value="Keep the proposal concise.",
    policy=writer_access,
)
budget = memory.put(type="project", value={"name": "Budget review"})  # owner_private: "app" only


# --- Agent side: it sees only the handle that it gets ---
class Agent:
    def __init__(self, memory: MemoryHandle) -> None:
        self.memory = memory

    # Tools that an LLM can call
    def memorize(self, text: str) -> str:
        self.memory.put(type="observation", value=text)
        return "Saved."

    def recall(self, query: str) -> str:
        results = self.memory.search(query)  # list[SearchResult]
        return "\n".join(f"- [{r.block.type}] {r.block.value}" for r in results)


agent = Agent(memory.get_handle(root=proposal, principal="agent:writer"))
print(agent.memorize("Use short sections."))  # Saved.
print(agent.recall("concise"))  # - [requirement] Keep the proposal concise.

# Handle scope: the budget project is outside the handle root.
try:
    agent.memory.get(budget)
except AccessDenied:
    print("Agent reads budget: AccessDenied")

# Policy: the budget project is owner_private, so "agent:writer" cannot get a handle to it.
try:
    memory.get_handle(root=budget, principal="agent:writer")
except AccessDenied:
    print("Handle on budget: AccessDenied")
