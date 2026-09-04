"""Latency statistics for one measured variant.

Every number here comes from `time.perf_counter_ns` durations collected by the
runner. Failed calls are counted but never timed, so one exception cannot be
read as a fast call.

Two rates are reported, and they answer different questions:

- **`ops_per_second`** is what one caller saw, from the time its own calls
  took. It ignores the gaps between calls and the other workers entirely.
- **`throughput_per_second`** is what every worker together achieved, from the
  wall time of the measured window. At one worker the two are close. At more
  than one they separate, and that separation is the point of a concurrent
  run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

NANOSECONDS_PER_MILLISECOND = 1_000_000
"""Divisor that turns a nanosecond duration into milliseconds."""

NANOSECONDS_PER_SECOND = 1_000_000_000
"""Divisor that turns a nanosecond duration into seconds."""


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Return a percentile using the nearest-rank method.

    Nearest rank means every returned value is a real measurement and not a
    point interpolated between two samples.

    Args:
        sorted_values: Values already sorted from smallest to largest.
        fraction: Share between `0.0` and `1.0`, such as `0.95` for p95.

    Returns:
        The value at that rank, or `0.0` when there are no values.

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
    """What one measured variant reported.

    Attributes:
        samples: Measured calls that returned without raising.
        errors: Measured calls that raised an unexpected exception.
        conflicts: Measured calls that lost a documented race, such as a
            `MemoryConflict` on a block another worker wrote first. A conflict
            is the answer a contention case asked for, not a failure, so it is
            counted apart from `errors` and never fails a run.
        first_error: Type and message of the first failure, when there was one.
        p50_ms: Median call latency in milliseconds.
        p95_ms: 95th percentile call latency in milliseconds.
        p99_ms: 99th percentile call latency in milliseconds.
        min_ms: Fastest measured call in milliseconds.
        max_ms: Slowest measured call in milliseconds.
        mean_ms: Average measured call in milliseconds.
        ops_per_second: Rate one caller saw, from the time its own calls took.
            Setup and warmup are excluded.
        throughput_per_second: Rate every worker together achieved, from the
            wall time of the measured window. Only calls that returned are
            counted, so a variant that conflicts often reports a lower rate
            rather than a flattering one.
        workers: Callers that ran this variant at the same time.
        wall_seconds: Wall time of the measured window. Warmup and setup are
            excluded.
        setup_seconds: Wall time spent building the corpus and the variant's
            own targets. Excluded from every latency number.
        notes: What the calls actually did, such as how many children a
            traversal returned or how many results a search returned.
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
    """Turn measured durations into a summary.

    Args:
        durations_ns: Durations of the calls that returned, in nanoseconds.
            At more than one worker this is every worker's durations merged
            into one pool.
        errors: Calls that raised an unexpected exception and were therefore
            not timed.
        conflicts: Calls that lost a documented race and were therefore not
            timed. Counted apart from `errors`, because a contention case
            expects them.
        first_error: Type and message of the first failure.
        workers: Callers that ran this variant at the same time.
        wall_seconds: Wall time of the measured window, used for
            `throughput_per_second`. Zero leaves that rate at `0.0`.
        setup_seconds: Wall time spent preparing the variant.
        notes: What the calls actually did.

    Returns:
        A summary. Every latency field is `0.0` when no call was timed, so a
        variant where everything failed or conflicted still reports rather than
        crashing.
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
