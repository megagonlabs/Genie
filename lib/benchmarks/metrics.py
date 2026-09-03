"""Latency statistics for one measured variant.

Every number here comes from `time.perf_counter_ns` durations collected by the
runner. Failed calls are counted but never timed, so one exception cannot be
read as a fast call.
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
        errors: Measured calls that raised.
        first_error: Type and message of the first failure, when there was one.
        p50_ms: Median call latency in milliseconds.
        p95_ms: 95th percentile call latency in milliseconds.
        p99_ms: 99th percentile call latency in milliseconds.
        min_ms: Fastest measured call in milliseconds.
        max_ms: Slowest measured call in milliseconds.
        mean_ms: Average measured call in milliseconds.
        ops_per_second: Rate one caller saw, from the time its own calls took.
            Setup and warmup are excluded.
        setup_seconds: Wall time spent building the corpus and the variant's
            own targets. Excluded from every latency number.
        notes: What the calls actually did, such as how many children a
            traversal returned or how many results a search returned.
    """

    samples: int = 0
    errors: int = 0
    first_error: str | None = None
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    ops_per_second: float = 0.0
    setup_seconds: float = 0.0
    notes: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the summary as plain data for the JSON report."""

        return {
            "samples": self.samples,
            "errors": self.errors,
            "first_error": self.first_error,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
            "ops_per_second": self.ops_per_second,
            "setup_seconds": self.setup_seconds,
            "notes": dict(self.notes),
        }


def summarize(
    durations_ns: Sequence[int],
    *,
    errors: int = 0,
    first_error: str | None = None,
    setup_seconds: float = 0.0,
    notes: Mapping[str, Any] | None = None,
) -> Summary:
    """Turn measured durations into a summary.

    Args:
        durations_ns: Durations of the calls that returned, in nanoseconds.
        errors: Calls that raised and were therefore not timed.
        first_error: Type and message of the first failure.
        setup_seconds: Wall time spent preparing the variant.
        notes: What the calls actually did.

    Returns:
        A summary. Every latency field is `0.0` when no call was timed, so a
        variant where everything failed still reports rather than crashing.
    """

    note_data = dict(notes or {})
    if not durations_ns:
        return Summary(
            errors=errors,
            first_error=first_error,
            setup_seconds=setup_seconds,
            notes=note_data,
        )
    milliseconds = sorted(duration / NANOSECONDS_PER_MILLISECOND for duration in durations_ns)
    total_seconds = sum(durations_ns) / NANOSECONDS_PER_SECOND
    return Summary(
        samples=len(milliseconds),
        errors=errors,
        first_error=first_error,
        p50_ms=percentile(milliseconds, 0.50),
        p95_ms=percentile(milliseconds, 0.95),
        p99_ms=percentile(milliseconds, 0.99),
        min_ms=milliseconds[0],
        max_ms=milliseconds[-1],
        mean_ms=sum(milliseconds) / len(milliseconds),
        ops_per_second=len(milliseconds) / total_seconds if total_seconds > 0 else 0.0,
        setup_seconds=setup_seconds,
        notes=note_data,
    )
