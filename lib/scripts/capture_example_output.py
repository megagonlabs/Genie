"""Regenerate the captured output of each runnable example.

Run it from the repository root:

    python lib/scripts/capture_example_output.py

- **Interpreter:** each example runs with the interpreter that started this
  script.
- **Output:** the script writes the standard output of each example under
  `examples/expected/`.
- **When to run:** `lib/tests/test_examples.py` compares a live run with those
  files. So run this script each time that the output of an example changes on
  purpose.
- **Unused files:** the script removes the captured output that no example
  owns.
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
    """Run one example and write its printed output.

    Args:
        example: The example path, relative to `examples/`.

    Returns:
        2 values: `True` if the example succeeded, and `True` if its captured
        output changed.
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
    """Delete the captured output that no example owns now.

    Args:
        examples: All discovered example paths, relative to `examples/`.

    Returns:
        The removed files.
    """

    if not EXPECTED_ROOT.exists():
        return []

    owned = {expected_path_for(example) for example in examples}
    removed = sorted(path for path in EXPECTED_ROOT.rglob("*.out") if path not in owned)
    for path in removed:
        path.unlink()
    return removed


def main() -> int:
    """Capture each example, one after the other, and report the changes.

    Returns:
        The process exit code. It is not 0 if an example failed.
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
