"""Machine and source facts recorded with every report.

Latency moves with the interpreter, the operating system, the machine, and the
code under test. A report without these facts cannot be compared with another
one, so they are collected once per run.
"""

from __future__ import annotations

import os
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
"""Repository root, used to read the current commit without calling git."""


def describe() -> dict[str, Any]:
    """Collect the facts a reader needs to compare two reports.

    Returns:
        Plain data holding the Python build, the operating system, the
        machine, the usable CPU count, the `genie` version, and the source
        commit when one can be read.
    """

    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "operating_system": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpu_count": _usable_cpu_count(),
        "genie_version": _genie_version(),
        "commit": read_commit(REPOSITORY_ROOT),
    }


def _genie_version() -> str:
    """Return the installed `genie` version, or `"unknown"` when unreadable."""

    try:
        return version("genie")
    except PackageNotFoundError:
        return "unknown"


def read_commit(root: Path) -> str | None:
    """Read the checked-out commit from the git directory.

    The commit is read from files rather than by running git, so the harness
    keeps its standard-library-only rule and starts no subprocess.

    Args:
        root: Repository root holding the `.git` directory.

    Returns:
        The short commit hash, or `None` when it cannot be read. A packed or
        unusual git layout is a missing answer, not an error.
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
    """Return the CPU count this process may actually use."""

    if hasattr(os, "process_cpu_count"):
        return os.process_cpu_count()
    return os.cpu_count()


def format_lines(environment: dict[str, Any]) -> list[str]:
    """Render the environment as short report header lines.

    Args:
        environment: Data returned by `describe`.

    Returns:
        One line naming the interpreter and machine, and one naming the
        library under test.
    """

    return [
        "{python_implementation} {python_version} on {operating_system} "
        "({machine}, {cpu_count} CPU)".format(**environment),
        "genie {genie_version} at commit {commit}".format(**environment),
    ]
