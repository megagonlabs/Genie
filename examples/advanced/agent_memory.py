"""Give an agent an application-friendly compound memory.

``TaskMemory`` hides structure roots and block creation while explicitly
composing three generic Genie facades:

- a keyed collection of task preconditions
- an ordered log of previous experiments
- a property graph connecting knowledge

Run from the repository root:

    conda run -n "genie" python examples/advanced/agent_memory.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, cast

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    InMemoryBackend,
    KeyedStore,
    Memory,
    MemoryBlock,
    MemoryHandle,
    SequenceStore,
    UnsupportedOperation,
)
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    OwnershipTree,
    PropertyGraph,
)

DATA_READY = "data_ready"


class Precondition(TypedDict):
    """Stored value for one named task precondition."""

    name: str
    satisfied: bool


@dataclass(frozen=True)
class Preconditions:
    """Read and write named precondition values."""

    _handle: MemoryHandle
    _collection: KeyedCollection

    def set(self, name: str, *, satisfied: bool) -> None:
        """Create or update one named precondition."""

        address = _precondition_address(name)
        current = self._collection.get(address)
        if current is None:
            value: Precondition = {
                "name": name.replace("_", " "),
                "satisfied": satisfied,
            }
            block = self._handle.put(type="precondition", value=value)
            self._collection.bind(address, block)
            return
        self._handle.update(current, value={"satisfied": satisfied})

    def get(self, name: str) -> Precondition | None:
        """Return a stored value, or ``None`` when the name is unknown."""

        block = self._get_block(name)
        if block is None:
            return None
        return cast(Precondition, block.value)

    def _get_block(self, name: str) -> MemoryBlock | None:
        """Return the block used internally by another memory view."""

        return self._collection.get(_precondition_address(name))


@dataclass(frozen=True)
class ExperimentHistory:
    """Read and write experiment values in chronological order."""

    _handle: MemoryHandle
    _log: EventLog

    def append(self, result: str) -> None:
        """Store an experiment result and append it to the ordered history."""

        block = self._handle.put(type="experiment", value=result)
        self._log.append(block)

    def tail(self, *, limit: int = 20) -> list[str]:
        """Return recent experiment values in chronological order."""

        return [item.block.value for item in self._log.tail(limit=limit)]


@dataclass(frozen=True)
class Knowledge:
    """Read and write graph relationships without interpreting them."""

    _handle: MemoryHandle
    _graph: PropertyGraph
    _preconditions: Preconditions

    def connect(self, subject: str, relation: str, target: str) -> None:
        """Connect a named precondition to a stored knowledge value."""

        source = self._preconditions._get_block(subject)
        if source is None:
            raise ValueError(f"Unknown precondition {subject!r}.")
        target_block = self._handle.put(type="knowledge", value=target)
        self._graph.add_edge(source, target_block, relation)

    def neighbors(self, subject: str, *, relation: str) -> list[str]:
        """Return all values related to a named precondition."""

        source = self._preconditions._get_block(subject)
        if source is None:
            return []
        return [
            neighbor.block.value
            for neighbor in self._graph.neighbors(source, relation=relation)
        ]


@dataclass(frozen=True)
class TaskMemory:
    """Application memory composed from three value-oriented read/write views."""

    preconditions: Preconditions
    experiments: ExperimentHistory
    knowledge: Knowledge

    @classmethod
    def create(cls, memory: Memory, *, workspace: MemoryBlock) -> "TaskMemory":
        """Create structure roots and return a value-oriented task memory."""

        backend = memory.backend
        if not isinstance(backend, KeyedStore):
            raise UnsupportedOperation("TaskMemory requires a keyed backend store.")
        keyed_store = backend
        if not isinstance(backend, SequenceStore):
            raise UnsupportedOperation("TaskMemory requires a sequence backend store.")
        sequence_store = backend

        handle = memory.get_handle(root=workspace)
        tree = OwnershipTree(handle, root=workspace)
        preconditions_root = tree.create_child(
            type="collection",
            value="preconditions",
        )
        experiments_root = tree.create_child(
            type="log",
            value="experiments",
        )
        keyed_collection = KeyedCollection(
            handle,
            keyed_store,
            collection=preconditions_root,
        )
        event_log = EventLog(handle, sequence_store, log=experiments_root)
        property_graph = PropertyGraph(handle)
        preconditions = Preconditions(handle, keyed_collection)
        return cls(
            preconditions=preconditions,
            experiments=ExperimentHistory(handle, event_log),
            knowledge=Knowledge(handle, property_graph, preconditions),
        )


@dataclass(frozen=True)
class TaskAgent:
    """Small application agent that reads its compound memory."""

    name: str
    memory: TaskMemory

    def choose_next_action(self) -> str:
        """Choose an action from preconditions, history, and graph knowledge."""

        precondition = self.memory.preconditions.get(DATA_READY)
        if precondition is None or precondition["satisfied"] is not True:
            return "wait for data"

        recent = self.memory.experiments.tail(limit=2)
        enabled_actions = self.memory.knowledge.neighbors(
            DATA_READY,
            relation="enables",
        )
        if not recent or not enabled_actions:
            return "collect more context"
        return enabled_actions[0]


def main() -> None:
    """Build a three-structure compound memory and give it to an agent."""

    print("== Compound memory for an agent ==")
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    workspace = memory.put(type="task", value="improve retrieval")
    task_memory = TaskMemory.create(memory, workspace=workspace)

    task_memory.preconditions.set(DATA_READY, satisfied=True)
    task_memory.experiments.append("baseline recall was 0.72")
    task_memory.experiments.append("reranker recall was 0.81")
    task_memory.knowledge.connect(
        DATA_READY,
        "enables",
        "test a larger candidate set",
    )

    agent = TaskAgent("retrieval-agent", task_memory)
    selected = agent.choose_next_action()

    print(f"agent: {agent.name}")
    print(f"keyed precondition: {task_memory.preconditions.get(DATA_READY)}")
    print(f"event log history: {task_memory.experiments.tail()}")
    print(
        "property graph knowledge: "
        f"{DATA_READY} --enables--> "
        f"{task_memory.knowledge.neighbors(DATA_READY, relation='enables')}"
    )
    print(f"agent's next action: {selected}")

    assert task_memory.experiments.tail(limit=1) == ["reranker recall was 0.81"]
    assert selected == "test a larger candidate set"


def _precondition_address(name: str) -> MemoryAddress:
    """Return the private keyed address for a task precondition name."""

    return MemoryAddress(("task",), name)


if __name__ == "__main__":
    main()
