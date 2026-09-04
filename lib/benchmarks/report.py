"""Turns a report into a table a person can read, or a JSON file.

Up to three tables per section, not one wide one:

- **A latency table** with one row per measured variant.
- **An overhead table** with one row per case that was measured at both
  layers, saying what the public `Memory` call adds over the raw backend call.
- **A concurrency table,** printed only when a run measured more than one
  worker, with one row per worker level of each case and layer.

The target is a section heading rather than a repeated column, and numbers
are right aligned so decimal points line up. Every column is always printed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    variants = sum(
        len(case.get("variants", {})) + len(case.get("concurrency", ()))
        for section in sections
        for case in section.get("cases", ())
    )
    cases = len({case["name"] for section in sections for case in section.get("cases", ())})
    lines = ["Genie benchmarks"]
    if environment:
        lines.append(
            "{python_implementation} {python_version} on {operating_system} "
            "({machine}, {cpu_count} CPU, {switch_interval_ms:.1f} ms switch interval)".format(
                **environment
            )
        )
        lines.append("genie {genie_version} at commit {commit}".format(**environment))
    if settings:
        lines.append("settings: " + " ".join(f"{key}={value}" for key, value in settings.items()))
    run_line = (
        f"run: {_plural(cases, 'case')}, {_plural(variants, 'variant')} "
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


def _plural(count: int, noun: str) -> str:
    """Render a count with its noun, adding an `s` only when one is needed."""

    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _section_lines(section: Mapping[str, Any]) -> list[str]:
    """Build one section: a heading, a latency table, and the overhead table."""

    name = section.get("name", "?")
    if section.get("skipped"):
        return [f"{name} -- skipped: {section['skipped']}"]
    lines = [f"{name} -- {section.get('storage', '')}".rstrip(), ""]
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
