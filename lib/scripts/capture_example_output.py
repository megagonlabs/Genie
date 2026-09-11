"""Regenerate the captured output of every runnable example.

Run from the repository root:

    python lib/scripts/capture_example_output.py

Each example runs with the interpreter that started this script, and its
standard output is written under `examples/expected/`. `lib/tests/test_examples.py`
compares a live run against those files, so run this whenever an example's
output changes on purpose. Captured output that no example owns is removed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples._catalog import (
    EXPECTED_ROOT,
    command_for,
    discover_examples,
    documented_command,
    expected_path_for,
)


def capture(example: str) -> tuple[bool, bool]:
    """Run one example and write what it printed.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        Whether the example succeeded, and whether its captured output changed.
    """

    result = subprocess.run(
        command_for(example, python=sys.executable),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  failed with exit code {result.returncode}")
        print((result.stdout + result.stderr).rstrip())
        return False, False

    target = expected_path_for(example)
    before = target.read_text(encoding="utf-8") if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.stdout, encoding="utf-8", newline="\n")
    return True, before != result.stdout


def remove_orphans(examples: tuple[str, ...]) -> list[Path]:
    """Delete captured output that no example owns any more.

    Args:
        examples: Every discovered example path relative to `examples/`.

    Returns:
        The files that were removed.
    """

    if not EXPECTED_ROOT.exists():
        return []

    owned = {expected_path_for(example) for example in examples}
    removed = sorted(path for path in EXPECTED_ROOT.rglob("*.out") if path not in owned)
    for path in removed:
        path.unlink()
    return removed


def main() -> int:
    """Capture every example in turn and report what changed.

    Returns:
        Process exit code. Non-zero when any example failed to run.
    """

    examples = discover_examples()
    print(f"Capturing {len(examples)} examples into {EXPECTED_ROOT.relative_to(REPO_ROOT)}/")

    failed: list[str] = []
    changed: list[str] = []
    for position, example in enumerate(examples, start=1):
        print(f"[{position:02d}/{len(examples)}] {documented_command(example)}", flush=True)
        succeeded, differs = capture(example)
        if not succeeded:
            failed.append(example)
        elif differs:
            changed.append(example)
            print("  captured output changed")

    for path in remove_orphans(examples):
        print(f"Removed unused {path.relative_to(REPO_ROOT)}")

    print(f"\n{len(examples) - len(failed)} captured, {len(changed)} changed, {len(failed)} failed")
    for example in failed:
        print(f"  failed: {example}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
