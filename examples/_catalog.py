"""Catalog of the runnable examples and the location of their captured output.

- **Writer:** `lib/scripts/capture_example_output.py` writes the captured output.
- **Checker:** `lib/tests/test_examples.py` compares live runs with the captured
  output.
- **One source of rules:** both tools read their rules from this module, so the
  rules in the 2 tools are always the same.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_ROOT.parent
EXPECTED_ROOT = EXAMPLES_ROOT / "expected"

SCRIPT_TIERS = ("basic", "advanced")
LAMPS_DIR = "lamps"
DEMO_NAME = "demo.py"


def discover_examples() -> tuple[str, ...]:
    """Find every runnable example under `examples/`.

    - **Scripts:** each `.py` file in `basic/` or `advanced/` is one example.
      The function skips `__init__.py`.
    - **Lamps:** a lamp is a package, so only its `demo.py` entry point is an
      example. The modules that the lamp imports are not examples.

    Returns:
        The example paths relative to `examples/`, in sorted order. For example,
        `basic/crud_blocks.py` and `lamps/mem0/demo.py`.
    """

    scripts = [
        path.relative_to(EXAMPLES_ROOT).as_posix()
        for tier in SCRIPT_TIERS
        for path in (EXAMPLES_ROOT / tier).glob("*.py")
        if path.name != "__init__.py"
    ]
    demos = [
        path.relative_to(EXAMPLES_ROOT).as_posix()
        for path in (EXAMPLES_ROOT / LAMPS_DIR).glob(f"*/{DEMO_NAME}")
    ]
    return tuple(sorted(scripts + demos))


def is_lamp_demo(example: str) -> bool:
    """Report if an example is a lamp demo and not a single script.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        `True` if the example is under `examples/lamps/`.
    """

    return example.startswith(f"{LAMPS_DIR}/")


def command_for(example: str, *, python: str) -> list[str]:
    """Build the documented command that runs one example.

    Args:
        example: Example path relative to `examples/`.
        python: The interpreter that runs the example.

    Returns:
        The argv to run from the repository root. A script runs by its path. A
        lamp demo runs as a module, because each lamp is a package.
    """

    if is_lamp_demo(example):
        module = f"examples.{example.removesuffix('.py').replace('/', '.')}"
        return [python, "-m", module]
    return [python, str(EXAMPLES_ROOT / example)]


def documented_command(example: str) -> str:
    """Return the command that the examples guides show to the reader.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        The command as the examples guides write it. It uses `python`, and a
        script path starts with `examples/`.
    """

    return " ".join(command_for(example, python="python")).replace(f"{EXAMPLES_ROOT}/", "examples/")


def expected_path_for(example: str) -> Path:
    """Return the captured-output file for one example.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        The path of the `.out` file under `examples/expected/`. The file of a
        lamp demo has the name of its lamp, so `lamps/mem0/demo.py` maps to
        `lamps/mem0.out`.
    """

    if is_lamp_demo(example):
        lamp = example.split("/")[1]
        return EXPECTED_ROOT / LAMPS_DIR / f"{lamp}.out"
    return EXPECTED_ROOT / f"{example.removesuffix('.py')}.out"
