"""Shared path setup for examples run before Genie is installed."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_on_path() -> None:
    """Allow examples to run before Genie is installed as a package.

    Puts two directories on `sys.path`: `lib/src`, so `import genie` works from a
    clone, and the repository root, so `import examples` works as well.
    """

    repo_root = Path(__file__).resolve().parents[1]
    for entry in (repo_root, repo_root / "lib" / "src"):
        entry_text = str(entry)
        if entry_text not in sys.path:
            sys.path.insert(0, entry_text)
