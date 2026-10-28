"""Machine and source facts that each report records.

- **Why:** latency changes with the interpreter, the operating system, the
  machine, and the code under test. Without these facts, you cannot compare a
  report with a different report. So the module collects them one time for
  each run.
- **Thread switch interval:** the module records it for the same reason. It
  sets how frequently the interpreter can interrupt a pure-Python call. So it
  has a larger effect on the conflict counts of a concurrent run than all other
  facts in this file.
"""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
"""The repository root. The module reads the current commit from it without a
call to git."""


def describe() -> dict[str, Any]:
    """Collect the facts that a reader needs to compare 2 reports.

    Returns:
        Plain data with the Python build, the operating system, the machine,
        the usable CPU count, the thread switch interval, the `genie` version,
        and the source commit if the function can read it.
    """

    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "operating_system": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpu_count": _usable_cpu_count(),
        "switch_interval_ms": sys.getswitchinterval() * 1000.0,
        "genie_version": _genie_version(),
        "commit": read_commit(REPOSITORY_ROOT),
    }


def _genie_version() -> str:
    """Return the installed `genie` version, or `"unknown"` if it cannot be read."""

    try:
        return version("genie")
    except PackageNotFoundError:
        return "unknown"


def read_commit(root: Path) -> str | None:
    """Read the checked-out commit from the git directory.

    The function reads the commit from files and does not run git. So the
    harness keeps its rule to use only the standard library, and it starts no
    subprocess.

    Args:
        root: The repository root that contains the `.git` directory.

    Returns:
        The short commit hash, or `None` if the function cannot read it. A
        packed or unusual git layout gives no answer. It is not an error.
    """

    git_directory = root / ".git"
    try:
        head = (git_directory / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head[:12] or None
    reference = head.removeprefix("ref: ").strip()
    try:
        commit = (git_directory / reference).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return commit[:12] or None


def _usable_cpu_count() -> int | None:
    """Return the number of CPUs that this process can use."""

    if sys.version_info >= (3, 13):
        return os.process_cpu_count()
    return os.cpu_count()


def format_lines(environment: dict[str, Any]) -> list[str]:
    """Show the environment as short header lines for the report.

    Args:
        environment: The data that `describe` returns.

    Returns:
        One line with the interpreter and the machine, and one line with the
        library under test.
    """

    return [
        "{python_implementation} {python_version} on {operating_system} "
        "({machine}, {cpu_count} CPU, {switch_interval_ms:.1f} ms switch interval)".format(
            **environment
        ),
        "genie {genie_version} at commit {commit}".format(**environment),
    ]
