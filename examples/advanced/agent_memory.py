"""Give an agent a compound memory made from three optional structures.

The agent receives one application-owned object containing:

- a keyed collection of task preconditions
- an ordered log of previous experiments
- a property graph connecting knowledge

Run from the repository root:

    conda run -n "genie" python examples/advanced/agent_memory.py
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

from genie import InMemoryBackend, Memory
from genie.structures import EventLog, KeyedCollection, MemoryAddress, PropertyGraph


DATA_READY = MemoryAddress(("task",), "data_ready")


@dataclass(frozen=True)
class TaskMemory:
    """The subset of memory structures available to one agent."""

    preconditions: KeyedCollection
    experiments: EventLog
    knowledge: PropertyGraph


@dataclass(frozen=True)
class TaskAgent:
    """Small application agent that reads its compound memory."""

    name: str
    memory: TaskMemory

    def choose_next_action(self) -> str:
        """Choose an action from preconditions, history, and graph knowledge."""

        precondition = self.memory.preconditions.get(DATA_READY)
        if precondition is None or not precondition.value["satisfied"]:
            return "wait for data"

        recent = self.memory.experiments.tail(limit=2)
        candidates = self.memory.knowledge.neighbors(
            precondition,
            relation="enables",
        )
        if not recent or not candidates:
            return "collect more context"
        return candidates[0].block.value


def main() -> None:
    """Build a three-structure compound memory and give it to an agent."""

    print("== Compound memory for an agent ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    workspace = memory.put(type="task", value="improve retrieval")
    preconditions_root = memory.put(
        parent=workspace,
        type="collection",
        value="preconditions",
    )
    experiments_root = memory.put(
        parent=workspace,
        type="log",
        value="experiments",
    )
    data_ready = memory.put(
        parent=workspace,
        type="precondition",
        value={"name": "data ready", "satisfied": True},
    )
    baseline = memory.put(
        parent=workspace,
        type="experiment",
        value="baseline recall was 0.72",
    )
    reranker = memory.put(
        parent=workspace,
        type="experiment",
        value="reranker recall was 0.81",
    )
    next_action = memory.put(
        parent=workspace,
        type="action",
        value="test a larger candidate set",
    )
    handle = memory.get_handle(root=workspace)
    compound = TaskMemory(
        preconditions=KeyedCollection(
            handle,
            backend,
            collection=preconditions_root,
        ),
        experiments=EventLog(handle, backend, log=experiments_root),
        knowledge=PropertyGraph(handle),
    )

    compound.preconditions.bind(DATA_READY, data_ready)
    compound.experiments.append(baseline)
    compound.experiments.append(reranker)
    compound.knowledge.add_edge(data_ready, next_action, "enables")

    agent = TaskAgent("retrieval-agent", compound)
    selected = agent.choose_next_action()

    print(f"agent: {agent.name}")
    print(f"keyed precondition: {data_ready.value}")
    print(f"event log history: {[item.block.value for item in compound.experiments.tail()]}")
    print(f"property graph recommendation: {next_action.value}")
    print(f"agent's next action: {selected}")

    assert selected == next_action.value


if __name__ == "__main__":
    main()
