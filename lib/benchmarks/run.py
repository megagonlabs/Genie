"""Command line entry point for the Genie benchmarks.

Run it from the repository root:

    conda run -n "genie" python lib/benchmarks/run.py
    conda run -n "genie" python lib/benchmarks/run.py --list
    conda run -n "genie" python lib/benchmarks/run.py --iterations 500 --blocks 10000
    conda run -n "genie" python lib/benchmarks/run.py --target sqlite --group read
    conda run -n "genie" python lib/benchmarks/run.py --layer backend --layer memory
    conda run -n "genie" python lib/benchmarks/run.py --workers 4 --workers 8
    conda run -n "genie" python lib/benchmarks/run.py --json reports/latest.json

Exit status is `1` when any measured call raised, and `0` otherwise. Timing is
never a pass or fail signal, because a shared machine is too noisy for that,
and a conflict a `contention` case expected is not an error.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - only when run as a plain script
    # `python lib/benchmarks/run.py` puts this file's directory on the path, not
    # its parent, so the `benchmarks` package would not be importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# These imports must stay below the path line above, or running this file
# directly cannot find the `benchmarks` package.
from benchmarks import report, runner
from benchmarks.cases import GROUPS, default_cases, select
from benchmarks.progress import for_stream
from benchmarks.settings import LAYERS, SINGLE_CALLER, BenchmarkSettings
from benchmarks.targets import default_targets

EXIT_OK = 0
"""Every measured call returned."""

EXIT_CALL_FAILED = 1
"""At least one measured call raised."""

EXIT_BAD_SELECTION = 2
"""A target, group, or case name on the command line matched nothing."""

NOISY_DRIVER_LOGGERS = ("neo4j.notifications",)
"""Driver loggers that write one record per query.

Neo4j reports a notification whenever a query names a label, property, or
relationship type the database has not stored yet. Every query against a store
a variant has just emptied does that, so the notification arrives on nearly
every call. The driver logs it while it streams the result, which is inside the
call this harness is timing, and Python's last-resort handler writes it to
standard error, which is where the progress line lives. Left alone it both
buries the progress line and inflates every `neo4j` number in the report.
"""

CONCURRENT_ONLY_HINT = (
    "the contention cases share one target between workers, so they need more than one caller. "
    "add --workers 4, or drop --group contention"
)
"""Said when a contention case was asked for without a worker level above one."""


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        The parser, with every flag documented in its help text.
    """

    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Measure what one Genie operation costs, one backend at a time.",
    )
    defaults = BenchmarkSettings()
    parser.add_argument("--iterations", type=int, default=defaults.iterations, help="Measured calls per variant")
    parser.add_argument("--warmup", type=int, default=defaults.warmup, help="Calls made and thrown away before measuring starts")
    parser.add_argument("--blocks", type=int, default=defaults.blocks, help="Leaf blocks in the corpus each variant runs against")
    parser.add_argument("--sections", type=int, default=defaults.sections, help="Blocks in the chain between the corpus root and its leaves")
    parser.add_argument("--value-size", type=int, default=defaults.value_size, help="Padding characters added to each generated value")
    parser.add_argument("--links-per-block", type=int, default=defaults.links_per_block, help="Outgoing links attached to the corpus hub block")
    parser.add_argument("--search-limit", type=int, default=defaults.search_limit, help="Candidate budget passed to search")
    parser.add_argument("--seed", type=int, default=defaults.seed, help="Seed for the corpus generator")
    parser.add_argument("--workers", type=int, action="append", default=None, help="Callers running one case at the same time. Repeatable. Level 1 is always measured")
    parser.add_argument("--layer", action="append", choices=LAYERS, default=None, help="Measure one layer only. Repeatable")
    parser.add_argument("--target", action="append", default=None, help="Measure one target only. Repeatable")
    parser.add_argument("--group", action="append", choices=GROUPS, default=None, help="Measure one group only. Repeatable")
    parser.add_argument("--case", action="append", default=None, help="Measure cases whose name contains this text. Repeatable")
    parser.add_argument("--json", default=None, help="Also write the full report to this path")
    parser.add_argument("--width", type=int, default=None, help="Characters to fit the tables into. 0 prints every column")
    parser.add_argument("--list", action="store_true", help="Print the targets and cases, then exit")
    parser.add_argument("--quiet", action="store_true", help="Write no progress lines and no table")
    return parser


def quiet_driver_logs() -> None:
    """Keep per-query driver log records out of a measured run.

    Only the loggers named in `NOISY_DRIVER_LOGGERS` are raised, and only to
    `ERROR`, so a real driver failure still reaches the stream. Nothing else in
    the process has its logging changed, because a benchmark run should not
    decide how an application logs.
    """

    for name in NOISY_DRIVER_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    """Run the benchmarks from command line arguments.

    Args:
        argv: Arguments to parse. Defaults to `sys.argv[1:]`.

    Returns:
        The process exit status.
    """

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        settings = BenchmarkSettings(
            iterations=arguments.iterations,
            warmup=arguments.warmup,
            blocks=arguments.blocks,
            sections=arguments.sections,
            value_size=arguments.value_size,
            links_per_block=arguments.links_per_block,
            search_limit=arguments.search_limit,
            seed=arguments.seed,
            workers=tuple(arguments.workers or (SINGLE_CALLER,)),
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    layers = tuple(arguments.layer or LAYERS)
    targets = default_targets()
    if arguments.target:
        known = {target.name for target in targets}
        unknown = sorted(set(arguments.target) - known)
        if unknown:
            print(
                f"unknown target {', '.join(unknown)}. known targets: {', '.join(sorted(known))}",
                file=sys.stderr,
            )
            return EXIT_BAD_SELECTION
        targets = tuple(target for target in targets if target.name in set(arguments.target))
    cases = select(
        default_cases(),
        groups=tuple(arguments.group or ()),
        names=tuple(arguments.case or ()),
        layers=layers,
        concurrent=settings.measures_concurrency,
    )
    if not cases:
        dropped = select(
            default_cases(),
            groups=tuple(arguments.group or ()),
            names=tuple(arguments.case or ()),
            layers=layers,
            concurrent=True,
        )
        # Naming the reason beats "matched nothing" when the selection was fine
        # and only the worker level was missing.
        message = CONCURRENT_ONLY_HINT if dropped else "no case matched the selection"
        print(message, file=sys.stderr)
        return EXIT_BAD_SELECTION
    if arguments.list:
        print(_listing(targets, cases, layers, settings.workers))
        return EXIT_OK
    quiet_driver_logs()
    result = runner.run(
        settings=settings,
        targets=targets,
        cases=cases,
        layers=layers,
        progress=for_stream(sys.stderr, quiet=arguments.quiet),
    )
    if arguments.json:
        written = report.write_json(result, arguments.json)
        print(f"wrote {written}", file=sys.stderr)
    if not arguments.quiet:
        print(report.format_report(result, width=_table_width(arguments.width)))
    return EXIT_CALL_FAILED if result["errors"] else EXIT_OK


def _table_width(requested: int | None) -> int | None:
    """Decide the width the tables are fitted into.

    A pipe or a file has no width to respect, so redirected output keeps every
    column unless `--width` asks otherwise.

    Args:
        requested: Value of `--width`, or `None` when it was not given.

    Returns:
        The width to fit into, or `None` to keep every column.
    """

    if requested is not None:
        return requested or None
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size().columns


def _listing(
    targets: tuple,
    cases: tuple,
    layers: tuple[str, ...],
    workers: tuple[int, ...],
) -> str:
    """Render what a run would measure, without measuring it."""

    lines = ["targets:"]
    for target in targets:
        state = target.skip_reason or "available"
        lines.append(f"  {target.name:12} {state}")
        lines.append(f"  {'':12} {target.storage}")
    lines.append("")
    lines.append(f"layers: {', '.join(layers)}")
    lines.append(f"workers: {', '.join(str(level) for level in workers)}")
    lines.append("")
    lines.append("cases:")
    for case in cases:
        measurable = ", ".join(layer for layer in case.layers if layer in layers)
        lines.append(f"  {case.group:9} {case.name:22} [{measurable}]")
        lines.append(f"  {'':9} {'':22} {case.description}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
