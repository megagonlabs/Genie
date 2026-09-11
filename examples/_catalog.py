"""Catalog of the runnable examples and where their captured output lives.

`lib/scripts/capture_example_output.py` writes the captured output and
`lib/tests/test_examples.py` compares live runs against it. Both read the rules
from this module so the two cannot drift apart.
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

    A script in `basic/` or `advanced/` is one example. A lamp is a package, so
    only its `demo.py` entry point counts, not the modules it imports.

    Returns:
        Sorted example paths relative to `examples/`, such as
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
    """Report whether an example is a lamp demo rather than a single script.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        True when the example lives under `examples/lamps/`.
    """

    return example.startswith(f"{LAMPS_DIR}/")


def command_for(example: str, *, python: str) -> list[str]:
    """Build the documented command that runs one example.

    Args:
        example: Example path relative to `examples/`.
        python: Interpreter to run the example with.

    Returns:
        The argv to run from the repository root. Scripts run by path. Lamp
        demos run as modules, because each lamp is a package.
    """

    if is_lamp_demo(example):
        module = f"examples.{example.removesuffix('.py').replace('/', '.')}"
        return [python, "-m", module]
    return [python, str(EXAMPLES_ROOT / example)]


def documented_command(example: str) -> str:
    """Return the command a reader would copy from the guides.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        The command as written in the examples guides.
    """

    return " ".join(command_for(example, python="python")).replace(f"{EXAMPLES_ROOT}/", "examples/")


def expected_path_for(example: str) -> Path:
    """Return the captured-output file for one example.

    Args:
        example: Example path relative to `examples/`.

    Returns:
        Path of the `.out` file under `examples/expected/`. A lamp demo is named
        after its lamp, so `lamps/mem0/demo.py` maps to `lamps/mem0.out`.
    """

    if is_lamp_demo(example):
        lamp = example.split("/")[1]
        return EXPECTED_ROOT / LAMPS_DIR / f"{lamp}.out"
    return EXPECTED_ROOT / f"{example.removesuffix('.py')}.out"
