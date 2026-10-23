"""Changes a report into tables that a person can read, or into a JSON file.

Each section has a maximum of 3 tables, not one wide table:

- **A latency table,** with one row for each measured variant.
- **An overhead table,** with one row for each case that has results at both
  layers. It shows the cost that the public `Memory` call adds to the raw
  backend call.
- **A concurrency table,** with one row for each worker level of each case and
  layer. The module prints it only if a run measured more than one worker.

A mixed run replaces all 3 tables with **a workload table for each worker
level**. This table has one row for each stream, one total row, and 2 heading
lines. The heading lines give the split and the 3 measured windows.

- **Layout:** the target is a section heading, not a repeated column.
- **Numbers:** they are right aligned, so the decimal points align.
- **Columns:** the module always prints all columns.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from benchmarks.settings import MEMORY

LATENCY_HEADERS = ("case", "layer", "p50 ms", "p95 ms", "p99 ms", "ops/s", "errors")
"""The latency table columns, in display order."""

OVERHEAD_HEADERS = ("case", "backend p50", "memory p50", "+ms", "+%")
"""The overhead table columns."""

CONCURRENCY_HEADERS = (
    "case",
    "layer",
    "workers",
    "p50 ms",
    "p95 ms",
    "wall ops/s",
    "speedup",
    "conflicts",
    "errors",
)
"""The concurrency table columns. `wall ops/s` is the rate of all workers
together. It is not the `ops/s` of each caller in the latency table."""

WORKLOAD_HEADERS = (
    "stream",
    "workers",
    "calls",
    "share %",
    "p50 ms",
    "p95 ms",
    "p99 ms",
    "wall ops/s",
    "alone p50",
    "+%",
    "conflicts",
    "errors",
)
"""The workload table columns.

- **`workers`:** the callers in that one stream. In the total row, it is the
  full level of the variant.
- **`alone p50`:** the same stream, measured when the other stream does not
  run.
- **`+%`:** the increase for the stream when the 2 streams run together.
- **No `+ms` column:** that absolute value is the difference of 2 columns
  that the table already shows.
"""

WORKLOAD_UNUSED_SETTINGS = ("iterations", "search_limit", "links_per_block")
"""The settings that a mixed run never reads.

- **Why:** a mixed worker stops at the end of the write sweep or at a repeated
  call count, never at `iterations`. No stream searches or follows a link, so
  the search limit and the hub links also have no effect.
- **Effect:** the header line does not show these settings. So it cannot show
  a value as if that value changed the result. The JSON file still records all
  settings.
"""

NUMERIC_HEADERS = frozenset(
    {
        "p50 ms",
        "p95 ms",
        "p99 ms",
        "ops/s",
        "wall ops/s",
        "workers",
        "speedup",
        "conflicts",
        "errors",
        "backend p50",
        "memory p50",
        "alone p50",
        "calls",
        "share %",
        "+ms",
        "+%",
    }
)
"""The columns with right alignment, so their decimal points align."""

COLUMN_GAP = "  "
"""The space between columns."""

MILLISECOND_FORMAT = ".4f"
"""The latency precision. The core helpers take less than one microsecond. With
3 decimals of a millisecond, they would show as 0."""


def write_json(report: Mapping[str, Any], path: str | Path) -> Path:
    """Write the full report as JSON.

    Args:
        report: The report that the runner returns.
        path: The file to write. The function creates the parent directories.

    Returns:
        The path of the written file.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def format_report(report: Mapping[str, Any]) -> str:
    """Show all of the report as text.

    Args:
        report: The report that the runner returns.

    Returns:
        The report as one string, without a newline at the end.
    """

    lines = _header_lines(report)
    for section in report.get("sections", ()):
        lines.append("")
        lines.extend(_section_lines(section))
    return "\n".join(lines)


def _header_lines(report: Mapping[str, Any]) -> list[str]:
    """Build the lines above the first section.

    Args:
        report: The report that the runner returns.

    Returns:
        The header lines.
    """

    environment = report.get("environment", {})
    settings = report.get("settings", {})
    sections: list[Any] = list(report.get("sections", ()))
    lines = ["Genie benchmarks"]
    if environment:
        lines.append(
            "{python_implementation} {python_version} on {operating_system} "
            "({machine}, {cpu_count} CPU, {switch_interval_ms:.1f} ms switch interval)".format(
                **environment
            )
        )
        lines.append("genie {genie_version} at commit {commit}".format(**environment))
    settings_line = _settings_line(report)
    if settings_line:
        lines.append(settings_line)
    if settings.get("workload"):
        variants = sum(len(section.get("workloads", ())) for section in sections)
        measured = f"mixed workload, {_plural(variants, 'variant')}"
    else:
        variants = sum(
            len(case.get("variants", {})) + len(case.get("concurrency", ()))
            for section in sections
            for case in section.get("cases", ())
        )
        cases = len({case["name"] for section in sections for case in section.get("cases", ())})
        measured = f"{_plural(cases, 'case')}, {_plural(variants, 'variant')}"
    run_line = (
        f"run: {measured} "
        f"in {_plural(len(sections), 'section')}, "
        f"{report.get('duration_seconds', 0.0):.1f}s, {_plural(report.get('errors', 0), 'error')}"
    )
    conflicts = report.get("conflicts", 0)
    if conflicts:
        # A conflict is the result that a contention case expects. So the line
        # names conflicts next to the errors, and does not add them to the
        # errors.
        run_line = f"{run_line}, {_plural(conflicts, 'conflict')}"
    lines.append(run_line)
    return lines


def _settings_line(report: Mapping[str, Any]) -> str | None:
    """Show the settings that a reader needs to compare 2 reports.

    For a mixed run, the function makes 2 changes, so the line shows what
    really occurred:

    - **Unused settings:** the line does not show the settings that a mixed
      run never reads.
    - **`workers`:** the line shows the measured levels, not the requested
      levels. The requested levels always include the single caller, and a
      mixed variant cannot use it.

    Args:
        report: The report that the runner returns.

    Returns:
        The line, or `None` if the report has no settings.
    """

    settings = dict(report.get("settings", {}))
    if not settings:
        return None
    if settings.get("workload"):
        for key in WORKLOAD_UNUSED_SETTINGS:
            settings.pop(key, None)
        settings["workers"] = list(report.get("workers", ()))
    return "settings: " + " ".join(f"{key}={value}" for key, value in settings.items())


def _plural(count: int, noun: str) -> str:
    """Show a count with its noun. Add an `s` only if the count is not 1."""

    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _section_lines(section: Mapping[str, Any]) -> list[str]:
    """Build one section: a heading, then its tables.

    A mixed section gets one workload table for each worker level, and no case
    tables, because a mixed run measures no cases.

    Args:
        section: One report section.

    Returns:
        The lines of the section.
    """

    name = section.get("name", "?")
    if section.get("skipped"):
        return [f"{name} -- skipped: {section['skipped']}"]
    lines = [f"{name} -- {section.get('storage', '')}".rstrip(), ""]
    workloads = section.get("workloads") or ()
    if workloads:
        for index, entry in enumerate(workloads):
            if index:
                lines.append("")
            lines.extend(_workload_heading(entry))
            lines.extend(_render_table(WORKLOAD_HEADERS, _workload_rows(entry)))
        return lines
    lines.extend(_render_table(LATENCY_HEADERS, _latency_rows(section)))
    overhead_rows = _overhead_rows(section)
    if overhead_rows:
        lines.append("")
        lines.append("overhead: what the Memory call adds over the raw backend call")
        lines.extend(_render_table(OVERHEAD_HEADERS, overhead_rows))
    concurrency_rows = _concurrency_rows(section)
    if concurrency_rows:
        lines.append("")
        lines.append("concurrency: how the same call behaves with more callers")
        lines.extend(_render_table(CONCURRENCY_HEADERS, concurrency_rows))
    return lines


def _latency_rows(section: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build one row for each measured variant."""

    rows: list[dict[str, str]] = []
    for case in section.get("cases", ()):
        for layer, result in case.get("variants", {}).items():
            rows.append(
                {
                    "case": case["name"],
                    "layer": layer,
                    "p50 ms": format(result["p50_ms"], MILLISECOND_FORMAT),
                    "p95 ms": format(result["p95_ms"], MILLISECOND_FORMAT),
                    "p99 ms": format(result["p99_ms"], MILLISECOND_FORMAT),
                    "ops/s": f"{result['ops_per_second']:.1f}",
                    "errors": str(result["errors"]),
                }
            )
    return rows


def _overhead_rows(section: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build one row for each case with results at both layers."""

    rows: list[dict[str, str]] = []
    for case in section.get("cases", ()):
        if case.get("overhead_ms") is None:
            continue
        variants = case["variants"]
        percent = case.get("overhead_percent")
        rows.append(
            {
                "case": case["name"],
                "backend p50": format(variants["backend"]["p50_ms"], MILLISECOND_FORMAT),
                "memory p50": format(variants["memory"]["p50_ms"], MILLISECOND_FORMAT),
                "+ms": format(case["overhead_ms"], MILLISECOND_FORMAT),
                "+%": "-" if percent is None else f"{percent:.1f}",
            }
        )
    return rows


def _concurrency_rows(section: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build the worker-level rows of one section.

    Each layer starts with its single-caller row, from the `variants` of the
    case. So a reader can see the changes from the extra workers without a
    second table.

    Args:
        section: One report section.

    Returns:
        One row for each worker level, or an empty list if the run measured
        only a single caller.
    """

    rows: list[dict[str, str]] = []
    for case in section.get("cases", ()):
        entries = case.get("concurrency") or ()
        variants = case.get("variants", {})
        seen: set[str] = set()
        for entry in entries:
            layer = entry["layer"]
            if layer not in seen:
                seen.add(layer)
                baseline = variants.get(layer)
                if baseline:
                    rows.append(_concurrency_row(case["name"], layer, baseline, 1.0))
            rows.append(_concurrency_row(case["name"], layer, entry["result"], entry["speedup"]))
    return rows


def _concurrency_row(
    name: str,
    layer: str,
    result: Mapping[str, Any],
    speedup: float | None,
) -> dict[str, str]:
    """Show one worker level of one case and layer.

    Args:
        name: The case name.
        layer: The layer of the measurement.
        result: The summary of that level.
        speedup: The wall-clock throughput divided by the throughput of the
            single caller, or `None` if no divisor exists.

    Returns:
        The row values, with the column names as keys.
    """

    return {
        "case": name,
        "layer": layer,
        "workers": str(result.get("workers", 1)),
        "p50 ms": format(result["p50_ms"], MILLISECOND_FORMAT),
        "p95 ms": format(result["p95_ms"], MILLISECOND_FORMAT),
        "wall ops/s": f"{result.get('throughput_per_second', 0.0):.1f}",
        "speedup": "-" if speedup is None else f"{speedup:.2f}",
        "conflicts": str(result.get("conflicts", 0)),
        "errors": str(result["errors"]),
    }


def _workload_heading(entry: Mapping[str, Any]) -> list[str]:
    """Build the 2 lines above the table of one mixed variant.

    - **First line:** the split.
    - **Second line:** all 3 measured windows and the cost of the sweep. This
      is the main result, and a reader wants it before the percentiles.

    Args:
        entry: One mixed variant from the `workloads` of a section.

    Returns:
        The 2 heading lines.
    """

    readers = entry.get("readers", 0)
    writers = entry.get("writers", 0)
    windows = entry.get("windows", {})
    ratio = entry.get("sweep_ratio")
    sweep = next(
        (stream["result"]["samples"] for stream in entry.get("streams", ()) if stream["stream"] == "write"),
        0,
    )
    windows_line = (
        f"one sweep of {_plural(sweep, 'block')}: {_window(windows.get('mixed'))} mixed, "
        f"{_window(windows.get('reads_alone'))} reads alone, "
        f"{_window(windows.get('writes_alone'))} writes alone"
    )
    if ratio is not None:
        windows_line = f"{windows_line} (sweep {ratio:.2f}x slower mixed)"
    split_line = (
        f"workload x{entry.get('workers', 0)}: {_plural(readers, 'reader')}, "
        f"{_plural(writers, 'writer')}, {MEMORY} layer"
    )
    return [split_line, windows_line]


def _workload_rows(entry: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build one row for each stream of one mixed variant, then a total row.

    Args:
        entry: One mixed variant from the `workloads` of a section.

    Returns:
        The rows, in report order.
    """

    rows: list[dict[str, str]] = []
    for stream in entry.get("streams", ()):
        result = stream["result"]
        baseline = stream.get("baseline")
        share = stream.get("share_percent")
        percent = stream.get("p50_delta_percent")
        rows.append(
            {
                "stream": stream["stream"],
                "workers": str(result.get("workers", 1)),
                "calls": str(result["samples"]),
                "share %": "-" if share is None else f"{share:.1f}",
                "p50 ms": format(result["p50_ms"], MILLISECOND_FORMAT),
                "p95 ms": format(result["p95_ms"], MILLISECOND_FORMAT),
                "p99 ms": format(result["p99_ms"], MILLISECOND_FORMAT),
                "wall ops/s": f"{result.get('throughput_per_second', 0.0):.1f}",
                "alone p50": "-" if not baseline else format(baseline["p50_ms"], MILLISECOND_FORMAT),
                "+%": "-" if percent is None else f"{percent:+.1f}",
                "conflicts": str(result["conflicts"]),
                "errors": str(result["errors"]),
            }
        )
    rows.append(_workload_total_row(entry))
    return rows


def _workload_total_row(entry: Mapping[str, Any]) -> dict[str, str]:
    """Build the total row of one mixed variant.

    The function fills only the columns that add up. A latency percentile of 2
    streams together does not describe a real operation. So those cells show
    `-`, and not a number without a meaning.

    Args:
        entry: One mixed variant from the `workloads` of a section.

    Returns:
        The row values, with the column names as keys.
    """

    results = [stream["result"] for stream in entry.get("streams", ())]
    row = {
        "stream": "total",
        "workers": str(entry.get("workers", 0)),
        "calls": str(sum(result["samples"] for result in results)),
        "share %": "100.0" if results else "-",
        "wall ops/s": f"{sum(result.get('throughput_per_second', 0.0) for result in results):.1f}",
        "conflicts": str(sum(result["conflicts"] for result in results)),
        "errors": str(sum(result["errors"] for result in results)),
    }
    for header in ("p50 ms", "p95 ms", "p99 ms", "alone p50", "+%"):
        row[header] = "-"
    return row


def _window(seconds: float | None) -> str:
    """Show one measured window in milliseconds."""

    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


def _render_table(headers: Sequence[str], rows: Sequence[Mapping[str, str]]) -> list[str]:
    """Show a table with all columns.

    Args:
        headers: The column names, in display order.
        rows: The row values, with the column names as keys.

    Returns:
        The table lines.
    """

    if not rows:
        return ["(nothing measured)"]
    widths = {header: _column_width(header, rows) for header in headers}
    lines = [_render_row({header: header for header in headers}, headers, widths)]
    lines.extend(_render_row(row, headers, widths) for row in rows)
    return lines


def _render_row(
    row: Mapping[str, str],
    headers: Sequence[str],
    widths: Mapping[str, int],
) -> str:
    """Show one row, with names left aligned and numbers right aligned."""

    cells = []
    for header in headers:
        value = row.get(header, "")
        size = widths[header]
        cells.append(value.rjust(size) if header in NUMERIC_HEADERS else value.ljust(size))
    return COLUMN_GAP.join(cells).rstrip()


def _column_width(header: str, rows: Sequence[Mapping[str, str]]) -> int:
    """Return the width that one column needs for its header and all of its values."""

    return max([len(header), *(len(row.get(header, "")) for row in rows)])
