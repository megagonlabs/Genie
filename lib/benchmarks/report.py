"""Turns a report into a table a person can read, or a JSON file.

Up to three tables per section, not one wide one:

- **A latency table** with one row per measured variant.
- **An overhead table** with one row per case that was measured at both
  layers, saying what the public `Memory` call adds over the raw backend call.
- **A concurrency table,** printed only when a run measured more than one
  worker, with one row per worker level of each case and layer.

A mixed run replaces all three with **a workload table per worker level**: one
row per stream, one total row, and two heading lines naming the split and the
three measured windows.

The target is a section heading rather than a repeated column, and numbers
are right aligned so decimal points line up. Every column is always printed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from benchmarks.settings import MEMORY

LATENCY_HEADERS = ("case", "layer", "p50 ms", "p95 ms", "p99 ms", "ops/s", "errors")
"""Latency table columns, widest report first."""

OVERHEAD_HEADERS = ("case", "backend p50", "memory p50", "+ms", "+%")
"""Overhead table columns."""

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
"""Concurrency table columns. `wall ops/s` is the rate every worker together
achieved, which is not the per-caller `ops/s` of the latency table."""

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
"""Workload table columns.

`workers` is the callers in that one stream, and the total row's `workers` is
the variant's whole level. `alone p50` is the same stream measured with the
other side switched off, and `+%` is what running together cost it. There is no
absolute `+ms` column, because it is the difference of two columns already
here.
"""

WORKLOAD_UNUSED_SETTINGS = ("iterations", "search_limit", "links_per_block")
"""Settings a mixed run never reads.

A mixed worker stops on the write sweep or on a repeated call count, never on
`iterations`. Neither of its streams searches or follows a link, so the search
budget and the hub links have no effect either. They are left out of the header
line so it cannot suggest a value that changed something, and the JSON still
records every setting.
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
"""Columns right aligned so their decimal points line up."""

COLUMN_GAP = "  "
"""Spacing between columns."""

MILLISECOND_FORMAT = ".4f"
"""Latency precision. The core helpers are sub-microsecond, so three decimals
of a millisecond would round them to zero."""


def write_json(report: Mapping[str, Any], path: str | Path) -> Path:
    """Write the full report as JSON.

    Args:
        report: Report returned by the runner.
        path: File to write. Parent directories are created.

    Returns:
        The path that was written.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def format_report(report: Mapping[str, Any]) -> str:
    """Render the whole report as text.

    Args:
        report: Report returned by the runner.

    Returns:
        The report as one string, without a trailing newline.
    """

    lines = _header_lines(report)
    for section in report.get("sections", ()):
        lines.append("")
        lines.extend(_section_lines(section))
    return "\n".join(lines)


def _header_lines(report: Mapping[str, Any]) -> list[str]:
    """Build the lines above the first section.

    Args:
        report: Report returned by the runner.

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
        # A conflict is the answer a contention case asked for, so it is named
        # next to the errors rather than folded into them.
        run_line = f"{run_line}, {_plural(conflicts, 'conflict')}"
    lines.append(run_line)
    return lines


def _settings_line(report: Mapping[str, Any]) -> str | None:
    """Render the settings a reader needs to compare two reports.

    A mixed run is adjusted twice, so the line describes what actually
    happened: the settings it never reads are left out, and `workers` shows the
    levels that were measured rather than the levels that were asked for, which
    always include the single caller a mixed variant cannot use.

    Args:
        report: Report returned by the runner.

    Returns:
        The line, or `None` when the report carries no settings.
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
    """Render a count with its noun, adding an `s` only when one is needed."""

    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _section_lines(section: Mapping[str, Any]) -> list[str]:
    """Build one section: a heading, then its tables.

    A mixed section gets one workload table per worker level and none of the
    case tables, because a mixed run measures no cases at all.

    Args:
        section: One report section.

    Returns:
        The section's lines.
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
    """Build one row per measured variant."""

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
    """Build one row per case measured at both layers."""

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

    Each layer opens with its single-caller row, taken from the case's
    `variants`, so a reader can see what the extra workers changed without
    looking at another table.

    Args:
        section: One report section.

    Returns:
        One row per worker level, or an empty list when the run measured a
        single caller only.
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
    """Render one worker level of one case and layer.

    Args:
        name: Case name.
        layer: Layer the level was measured at.
        result: Summary of that level.
        speedup: Wall-clock throughput divided by the single caller's, or
            `None` when there is nothing to divide by.

    Returns:
        The row values keyed by column name.
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
    """Build the two lines above one mixed variant's table.

    The first names the split. The second names all three measured windows and
    what the sweep cost, which is the headline a reader wants before any
    percentile.

    Args:
        entry: One mixed variant from a section's `workloads`.

    Returns:
        The two heading lines.
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
    """Build one row per stream of one mixed variant, then a total row.

    Args:
        entry: One mixed variant from a section's `workloads`.

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

    Only the columns that add up are filled. A latency percentile of two
    streams together would describe no real operation, so those cells stay
    empty rather than carrying a number nobody should read.

    Args:
        entry: One mixed variant from a section's `workloads`.

    Returns:
        The row values keyed by column name.
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
    """Render one measured window in milliseconds."""

    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


def _render_table(headers: Sequence[str], rows: Sequence[Mapping[str, str]]) -> list[str]:
    """Render a table with every column.

    Args:
        headers: Column names in display order.
        rows: Row values keyed by column name.

    Returns:
        The rendered lines.
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
    """Render one row, names left aligned and numbers right aligned."""

    cells = []
    for header in headers:
        value = row.get(header, "")
        size = widths[header]
        cells.append(value.rjust(size) if header in NUMERIC_HEADERS else value.ljust(size))
    return COLUMN_GAP.join(cells).rstrip()


def _column_width(header: str, rows: Sequence[Mapping[str, str]]) -> int:
    """Return the width one column needs for its header and every value."""

    return max([len(header), *(len(row.get(header, "")) for row in rows)])
