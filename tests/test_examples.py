"""Smoke tests for runnable example scripts."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("example", "expected_heading"),
    [
        ("basic/crud_blocks.py", "CRUD and ownership"),
        ("basic/scoped_agents.py", "Scoped agent handles"),
        ("basic/search_filters.py", "Search and filters"),
        ("basic/links.py", "Links and mounts"),
        ("basic/routed_backend.py", "Routed storage and mounts"),
        ("basic/policies.py", "Access policies"),
        ("basic/hooks_codecs.py", "Hooks and codecs"),
        ("basic/stdlib_codecs.py", "Standard-library codecs"),
        ("basic/basic_usage.py", "Integrated agent memory walkthrough"),
        ("advanced/temporal_collection.py", "Temporal collection"),
        ("advanced/agent_memory.py", "Compound memory for an agent"),
        ("advanced/experiment_memory.py", "Experiment memory using all seven structures"),
    ],
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
