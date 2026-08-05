"""Shared path setup for examples run before Genie is installed."""

from __future__ import annotations

from pathlib import Path
import sys


def ensure_repo_on_path() -> None:
    """Allow examples to run before Genie is installed as a package."""

    repo_root = Path(__file__).resolve().parents[1]
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
