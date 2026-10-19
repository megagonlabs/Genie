"""Shared `sys.path` configuration for examples that run without an installed Genie package."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_on_path() -> None:
    """Let the examples run when Genie is not installed as a package.

    The function adds 2 directories to the start of `sys.path`. It skips a
    directory that is already in `sys.path`.

    - **`lib/src`:** `import genie` works from a clone.
    - **Repository root:** `import examples` works.
    """

    repo_root = Path(__file__).resolve().parents[1]
    for entry in (repo_root, repo_root / "lib" / "src"):
        entry_text = str(entry)
        if entry_text not in sys.path:
            sys.path.insert(0, entry_text)
