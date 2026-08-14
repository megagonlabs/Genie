"""Smoke tests for runnable example scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from examples.advanced.agent_memory import TaskAgent, TaskMemory
from genie import InMemoryBackend, Memory

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNABLE_EXAMPLES = [
    ("basic/crud_blocks.py", "CRUD and ownership"),
    ("basic/scoped_agents.py", "Scoped agent handles"),
    ("basic/search_filters.py", "Search and filters"),
    ("basic/links.py", "Links and mounts"),
    ("basic/routed_backend.py", "Routed storage and mounts"),
    ("basic/policies.py", "Access policies"),
    ("basic/hooks_codecs.py", "Hooks and codecs"),
    ("basic/stdlib_codecs.py", "Standard-library codecs"),
    ("basic/sqlite_persistence.py", "SQLite persistence"),
    ("basic/basic_usage.py", "Integrated agent memory walkthrough"),
    ("advanced/temporal_collection.py", "Temporal collection"),
    ("advanced/agent_memory.py", "Compound memory for an agent"),
    ("advanced/experiment_memory.py", "Experiment memory using all seven structures"),
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


@pytest.mark.parametrize(
    ("example", "expected_heading"),
    RUNNABLE_EXAMPLES,
)
def test_example_runs_as_a_script(example: str, expected_heading: str) -> None:
    """Every example script runs successfully from the repository root."""

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / example)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"== {expected_heading} ==\n")


def test_every_example_script_has_smoke_coverage() -> None:
    """Keep newly added basic and advanced scripts in the runnable test set."""

    examples_root = REPO_ROOT / "examples"
    discovered = {
        str(path.relative_to(examples_root))
        for tier in ("basic", "advanced")
        for path in (examples_root / tier).glob("*.py")
        if path.name != "__init__.py"
    }

    assert {example for example, _ in RUNNABLE_EXAMPLES} == discovered
