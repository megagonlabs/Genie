"""Latency statistics for one measured variant.

- **Source:** each number comes from the `time.perf_counter_ns` durations that
  the runner collects.
- **Failed calls:** the runner counts them but never times them. So one
  exception cannot look like a fast call.

The module reports 2 rates. They answer different questions:

- **`ops_per_second`** is the rate that one caller saw, from the duration of
  its own calls. It ignores the gaps between calls, and it ignores the other
  workers.
- **`throughput_per_second`** is the rate of all workers together, from the
  wall time of the measured window. With one worker, the 2 rates are almost
  the same. With more than one worker, they become different. This difference
  is the purpose of a concurrent run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

NANOSECONDS_PER_MILLISECOND = 1_000_000
"""The divisor that changes a nanosecond duration into milliseconds."""

NANOSECONDS_PER_SECOND = 1_000_000_000
"""The divisor that changes a nanosecond duration into seconds."""


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Return a percentile with the nearest-rank method.

    With the nearest-rank method, each returned value is a real measurement. It
    is not a value interpolated between 2 samples.

    Args:
        sorted_values: The values, already sorted from smallest to largest.
        fraction: A share from `0.0` to `1.0`, for example `0.95` for p95.

    Returns:
        The value at that rank, or `0.0` if there are no values.

    Raises:
        ValueError: If `fraction` is outside `0.0` to `1.0`.
    """

    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0.0 and 1.0.")
    if not sorted_values:
        return 0.0
    rank = math.ceil(fraction * len(sorted_values))
    index = min(max(rank - 1, 0), len(sorted_values) - 1)
    return float(sorted_values[index])


@dataclass(frozen=True, slots=True)
class Summary:
    """The results of one measured variant.

    Attributes:
        samples: The measured calls that returned without an exception.
        errors: The measured calls that raised an unexpected exception.
        conflicts: The measured calls that lost a documented race, for example
            a `MemoryConflict` on a block that another worker wrote first. A
            conflict is the result that a contention case expects, not a
            failure. So the summary counts it separately from `errors`, and it
            never causes a failed run.
        first_error: The type and message of the first failure, if a failure
            occurred.
        p50_ms: The median call latency in milliseconds.
        p95_ms: The 95th percentile call latency in milliseconds.
        p99_ms: The 99th percentile call latency in milliseconds.
        min_ms: The fastest measured call in milliseconds.
        max_ms: The slowest measured call in milliseconds.
        mean_ms: The average measured call in milliseconds.
        ops_per_second: The rate that one caller saw, from the duration of its
            own calls. It does not include setup and warmup.
        throughput_per_second: The rate of all workers together, from the wall
            time of the measured window. It counts only the calls that
            returned. So a variant with many conflicts reports a lower rate,
            not a rate that looks better than the real rate.
        workers: The callers that ran this variant at the same time.
        wall_seconds: The wall time of the measured window. It does not include
            warmup and setup.
        setup_seconds: The wall time to build the corpus and the targets that
            the variant prepares for itself. No latency number includes it.
        notes: What the calls did, for example the number of children that a
            traversal returned or the number of results that a search
            returned.
    """

    samples: int = 0
    errors: int = 0
    conflicts: int = 0
    first_error: str | None = None
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    ops_per_second: float = 0.0
    throughput_per_second: float = 0.0
    workers: int = 1
    wall_seconds: float = 0.0
    setup_seconds: float = 0.0
    notes: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the summary as plain data for the JSON report."""

        return {
            "samples": self.samples,
            "errors": self.errors,
            "conflicts": self.conflicts,
            "first_error": self.first_error,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
            "ops_per_second": self.ops_per_second,
            "throughput_per_second": self.throughput_per_second,
            "workers": self.workers,
            "wall_seconds": self.wall_seconds,
            "setup_seconds": self.setup_seconds,
            "notes": dict(self.notes),
        }


def summarize(
    durations_ns: Sequence[int],
    *,
    errors: int = 0,
    conflicts: int = 0,
    first_error: str | None = None,
    workers: int = 1,
    wall_seconds: float = 0.0,
    setup_seconds: float = 0.0,
    notes: Mapping[str, Any] | None = None,
) -> Summary:
    """Change measured durations into a summary.

    Args:
        durations_ns: The durations of the calls that returned, in
            nanoseconds. With more than one worker, this is one pool of the
            durations of all workers.
        errors: The calls that raised an unexpected exception. The runner did
            not time them.
        conflicts: The calls that lost a documented race. The runner did not
            time them. They are separate from `errors`, because a contention
            case expects them.
        first_error: The type and message of the first failure.
        workers: The callers that ran this variant at the same time.
        wall_seconds: The wall time of the measured window, for
            `throughput_per_second`. If it is 0, that rate stays `0.0`.
        setup_seconds: The wall time to prepare the variant.
        notes: What the calls did.

    Returns:
        A summary. If the runner timed no call, each latency field is `0.0`.
        So a variant in which all calls failed or had a conflict still gives a
        report, and does not crash.
    """

    note_data = dict(notes or {})
    if not durations_ns:
        return Summary(
            errors=errors,
            conflicts=conflicts,
            first_error=first_error,
            workers=workers,
            wall_seconds=wall_seconds,
            setup_seconds=setup_seconds,
            notes=note_data,
        )
    milliseconds = sorted(duration / NANOSECONDS_PER_MILLISECOND for duration in durations_ns)
    total_seconds = sum(durations_ns) / NANOSECONDS_PER_SECOND
    return Summary(
        samples=len(milliseconds),
        errors=errors,
        conflicts=conflicts,
        first_error=first_error,
        p50_ms=percentile(milliseconds, 0.50),
        p95_ms=percentile(milliseconds, 0.95),
        p99_ms=percentile(milliseconds, 0.99),
        min_ms=milliseconds[0],
        max_ms=milliseconds[-1],
        mean_ms=sum(milliseconds) / len(milliseconds),
        ops_per_second=len(milliseconds) / total_seconds if total_seconds > 0 else 0.0,
        throughput_per_second=len(milliseconds) / wall_seconds if wall_seconds > 0 else 0.0,
        workers=workers,
        wall_seconds=wall_seconds,
        setup_seconds=setup_seconds,
        notes=note_data,
    )
