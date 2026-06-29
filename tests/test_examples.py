"""Smoke tests for runnable example scripts."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "example",
    [
        "crud_blocks.py",
        "scoped_agents.py",
        "search_filters.py",
        "links.py",
        "policies.py",
        "hooks_codecs.py",
        "basic_usage.py",
    ],
)
def test_example_runs_as_a_script(example: str) -> None:
    """Every example script runs successfully from the repository root."""

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / example)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
