"""The command line entry point for the Genie benchmarks.

Run it from the repository root:

    python lib/benchmarks/run.py
    python lib/benchmarks/run.py --list
    python lib/benchmarks/run.py --iterations 500 --blocks 10000
    python lib/benchmarks/run.py --target sqlite --group read
    python lib/benchmarks/run.py --layer backend --layer memory
    python lib/benchmarks/run.py --workers 4 --workers 8
    python lib/benchmarks/run.py --workload --workers 4
    python lib/benchmarks/run.py --json reports/latest.json

- **Exit status `0`:** all measured calls returned.
- **Exit status `1`:** a measured call raised an exception. The duration of a
  call is never a pass or fail signal, because the noise on a shared machine is
  too large. A conflict that a `contention` case expects is not an error.
- **Exit status `2`:** a usage error, for example an unknown target or a
  selection that matches no case.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - only when run as a plain script
    # `python lib/benchmarks/run.py` puts the directory of this file on the
    # path, not its parent. Without this line, Python cannot import the
    # `benchmarks` package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# These imports stay below the path line above. If they move above it, a
# direct run of this file cannot find the `benchmarks` package.
from benchmarks import report, runner
from benchmarks.cases import GROUPS, default_cases, select
from benchmarks.progress import for_stream
from benchmarks.settings import LAYERS, MEMORY, SINGLE_CALLER, BenchmarkSettings
from benchmarks.targets import BackendTarget, default_targets
from benchmarks.workload import READ_PER_WRITE, split_roles

EXIT_OK = 0
"""All measured calls returned."""

EXIT_CALL_FAILED = 1
"""A minimum of one measured call raised an exception."""

EXIT_BAD_SELECTION = 2
"""The command line selected nothing that can run.

Examples are a target, group, or case name that matches nothing, and a case
selection that the run cannot do.
"""

NOISY_DRIVER_LOGGERS = ("neo4j.notifications",)
"""Driver loggers that write one record for each query.

- **Cause:** Neo4j sends a notification each time a query names a label,
  property, or relationship type that the database does not have yet. All
  queries on a store that a variant just emptied do this. So almost all calls
  get the notification.
- **Effect on the measurement:** the driver logs the notification while it
  streams the result. This occurs in the call that the harness times.
- **Effect on output:** the last-resort handler of Python writes the
  notification to standard error, which also has the progress line.
- **Result:** without a change, the notifications hide the progress line and
  make each `neo4j` number in the report larger.
"""

CONCURRENT_ONLY_HINT = (
    "the contention cases share one target between workers, so they need more than one caller. "
    "add --workers 4, or drop --group contention"
)
"""The message if the run selects a contention case without a worker level above one."""

WORKLOAD_SELECTION_HINT = (
    "--workload measures no cases, so --layer, --group, and --case do not apply to it. "
    "drop them, or drop --workload"
)
"""The message if the run gets a case selection together with the mixed workload.

A run with no cases cannot use a case selection. So the selection stops the
run. The run does not ignore it without a message.
"""

WORKLOAD_UNUSED_HINT = (
    "--workload takes its length from --blocks and reads neither links nor search, "
    "so --iterations, --search-limit, and --links-per-block have no effect on it"
)
"""The message at the start of each mixed run.

The run accepts these flags, because they have defaults, and a caller does not
have to remove them. But a run that ignores a number from the caller without a
message is worse than a run that tells the caller.
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        The parser. Its help text documents each flag.
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
    parser.add_argument(
        "--workload",
        action="store_true",
        help=f"Measure the mixed read/write scenario instead of the cases: {READ_PER_WRITE} readers per writer over one full write sweep. Needs --workers 4 or more",
    )
    parser.add_argument("--layer", action="append", choices=LAYERS, default=None, help="Measure one layer only. Repeatable")
    parser.add_argument("--target", action="append", default=None, help="Measure one target only. Repeatable")
    parser.add_argument("--group", action="append", choices=GROUPS, default=None, help="Measure one group only. Repeatable")
    parser.add_argument("--case", action="append", default=None, help="Measure cases whose name contains this text. Repeatable")
    parser.add_argument("--json", default=None, help="Also write the full report to this path")
    parser.add_argument("--list", action="store_true", help="Print the targets and cases, then exit")
    parser.add_argument("--quiet", action="store_true", help="Write no progress lines and no table")
    return parser


def quiet_driver_logs() -> None:
    """Keep the driver log records for each query out of a measured run.

    - **Scope:** the function sets only the loggers in `NOISY_DRIVER_LOGGERS`,
      and only to `ERROR`. So a real driver failure still goes to the stream.
    - **No other change:** the function does not change other loggers in the
      process. A benchmark run does not decide how an application logs.
    """

    for name in NOISY_DRIVER_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    """Run the benchmarks from command line arguments.

    Args:
        argv: The arguments to parse. The default is `sys.argv[1:]`.

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
            workload=arguments.workload,
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
    if settings.workload:
        return _run_workload(arguments, settings, targets)
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
        # If the selection is correct and only the worker level is not there,
        # the real reason is more useful than "matched nothing".
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
        print(report.format_report(result))
    return EXIT_CALL_FAILED if result["errors"] else EXIT_OK


def _run_workload(
    arguments: argparse.Namespace,
    settings: BenchmarkSettings,
    targets: tuple[BackendTarget, ...],
) -> int:
    """Measure the mixed read/write scenario and print its report.

    - **Case selection:** the mixed scenario measures no cases. So the function
      cannot use a case selection, and the selection stops the run.
    - **Unused size flags:** the function accepts the size flags that the
      scenario does not read. It names these flags, and does not ignore them
      without a message.

    Args:
        arguments: The parsed command line arguments.
        settings: The run settings, already validated, with `workload` set.
        targets: The targets to measure, after `--target` applied.

    Returns:
        The process exit status.
    """

    if arguments.layer or arguments.group or arguments.case:
        print(WORKLOAD_SELECTION_HINT, file=sys.stderr)
        return EXIT_BAD_SELECTION
    if arguments.list:
        print(_workload_listing(targets, settings))
        return EXIT_OK
    print(WORKLOAD_UNUSED_HINT, file=sys.stderr)
    quiet_driver_logs()
    result = runner.run(
        settings=settings,
        targets=targets,
        progress=for_stream(sys.stderr, quiet=arguments.quiet),
    )
    if arguments.json:
        written = report.write_json(result, arguments.json)
        print(f"wrote {written}", file=sys.stderr)
    if not arguments.quiet:
        print(report.format_report(result))
    return EXIT_CALL_FAILED if result["errors"] else EXIT_OK


def _workload_listing(
    targets: tuple[BackendTarget, ...],
    settings: BenchmarkSettings,
) -> str:
    """Show what a mixed run measures, but do not measure it.

    Args:
        targets: The targets that the run measures.
        settings: The run settings, with `workload` set.

    Returns:
        The listing as one string.
    """

    lines = ["targets:"]
    for target in targets:
        state = target.skip_reason or "available"
        lines.append(f"  {target.name:12} {state}")
        lines.append(f"  {'':12} {target.storage}")
    lines.append("")
    lines.append(f"workload: {READ_PER_WRITE} readers per writer, {MEMORY} layer")
    lines.append(f"sweep: {settings.blocks} blocks rewritten once per variant")
    lines.append("")
    lines.append("levels:")
    for level in settings.workload_levels:
        roles = split_roles(level)
        # The names agree with the stream column of the report. So a level here
        # and a row in the report have the same meaning.
        lines.append(f"  {level:3} workers   {roles.readers} read + {roles.writers} write")
    lines.append("")
    lines.append("passes, per level and target:")
    for name in runner.WORKLOAD_PASSES:
        lines.append(f"  {name:13} {runner.WORKLOAD_PASS_NOTES[name]}")
    return "\n".join(lines)


def _listing(
    targets: tuple,
    cases: tuple,
    layers: tuple[str, ...],
    workers: tuple[int, ...],
) -> str:
    """Show what a run measures, but do not measure it."""

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
