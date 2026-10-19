"""Smoke tests for runnable example scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from examples._catalog import (
    EXPECTED_ROOT,
    command_for,
    discover_examples,
    documented_command,
    expected_path_for,
)
from examples.advanced.agent_memory import TaskAgent, TaskMemory
from genie import InMemoryBackend, Memory

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SCRIPT = "python lib/scripts/capture_example_output.py"
RUNNABLE_EXAMPLES = [
    "basic/crud_blocks.py",
    "basic/block_presence.py",
    "basic/scoped_agents.py",
    "basic/search_filters.py",
    "basic/links.py",
    "basic/acyclic_links.py",
    "basic/mounted_handles.py",
    "basic/routed_backend.py",
    "basic/policies.py",
    "basic/hook_audit.py",
    "basic/write_hooks.py",
    "basic/search_hooks.py",
    "basic/custom_codecs.py",
    "basic/stdlib_codecs.py",
    "basic/sqlite_persistence.py",
    "basic/heterogeneous_backend.py",
    "basic/basic_usage.py",
    "advanced/temporal_collection.py",
    "advanced/graph_traversal.py",
    "advanced/versioned_catalog.py",
    "advanced/facade_containers.py",
    "advanced/structure_hooks.py",
    "advanced/agent_memory.py",
    "advanced/experiment_memory.py",
    "lamps/graphiti/demo.py",
    "lamps/hindsight/demo.py",
    "lamps/langmem/demo.py",
    "lamps/mem0/demo.py",
    "lamps/memoryos/demo.py",
]


def test_task_memory_exposes_values_while_hiding_facade_setup() -> None:
    """Application code writes and reads values through the compound memory."""

    memory = Memory(backend=InMemoryBackend())
    workspace = memory.put(type="task", value="improve retrieval")
    task_memory = TaskMemory.create(memory, workspace=workspace)

    task_memory.preconditions.set("data_ready", satisfied=True)
    task_memory.experiments.append("collect more samples")
    task_memory.knowledge.connect(
        "data_ready",
        "enables",
        "run the next experiment",
    )

    assert task_memory.preconditions.get("data_ready") == {
        "name": "data ready",
        "satisfied": True,
    }
    assert task_memory.preconditions.get("unknown") is None
    assert task_memory.experiments.tail(limit=1) == ["collect more samples"]
    assert task_memory.knowledge.neighbors("data_ready", relation="enables") == [
        "run the next experiment"
    ]

    task_memory.preconditions.set("data_ready", satisfied=False)
    assert task_memory.preconditions.get("data_ready") == {
        "name": "data ready",
        "satisfied": False,
    }


def test_task_agent_interprets_memory_and_selects_an_action() -> None:
    """Task decisions remain on the agent side of the memory boundary."""

    memory = Memory(backend=InMemoryBackend())
    workspace = memory.put(type="task", value="improve retrieval")
    task_memory = TaskMemory.create(memory, workspace=workspace)
    agent = TaskAgent("retrieval-agent", task_memory)

    assert agent.choose_next_action() == "wait for data"

    task_memory.preconditions.set("data_ready", satisfied=True)
    assert agent.choose_next_action() == "collect more context"

    task_memory.experiments.append("collect more samples")
    task_memory.knowledge.connect("data_ready", "enables", "try reranking")
    task_memory.knowledge.connect("data_ready", "enables", "expand the query")

    assert task_memory.knowledge.neighbors("data_ready", relation="enables") == [
        "try reranking",
        "expand the query",
    ]
    assert agent.choose_next_action() == "try reranking"

@pytest.mark.parametrize("example", RUNNABLE_EXAMPLES)
def test_example_matches_its_captured_output(example: str) -> None:
    """Every example runs from the repository root and prints its captured output."""

    expected = expected_path_for(example)
    assert expected.exists(), f"{example} has no captured output. Create it with: {CAPTURE_SCRIPT}"

    result = subprocess.run(
        command_for(example, python=sys.executable),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{documented_command(example)} failed:\n{result.stdout}{result.stderr}"
    assert result.stdout == expected.read_text(encoding="utf-8"), (
        f"{example} no longer prints what {expected.relative_to(REPO_ROOT)} holds. "
        f"If the change is intended, refresh it with: {CAPTURE_SCRIPT}"
    )


def test_every_example_script_has_smoke_coverage() -> None:
    """`RUNNABLE_EXAMPLES` lists each example script and lamp demo exactly once, new ones included."""

    assert len(RUNNABLE_EXAMPLES) == len(set(RUNNABLE_EXAMPLES))
    assert set(RUNNABLE_EXAMPLES) == set(discover_examples())


def test_no_orphan_captured_output() -> None:
    """Every captured-output file belongs to an example that still exists."""

    owned = {expected_path_for(example) for example in discover_examples()}
    orphans = sorted(path.relative_to(REPO_ROOT).as_posix() for path in EXPECTED_ROOT.rglob("*.out") if path not in owned)

    assert not orphans, f"Captured output with no example: {orphans}. Remove it with: {CAPTURE_SCRIPT}"
