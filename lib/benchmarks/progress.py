"""Live progress for a run in flight.

A benchmark run can spend a minute inside one case. Without output that reads
as a hang, so the reporter shows movement: calls finished, seconds spent, and a
rough estimate of what is left.

Two rules shape this module:

- **The output style matches the stream.** A terminal gets one line rewritten
  in place several times a second. A pipe, a file, or a CI log gets whole lines
  written far apart, because nothing there can erase a line and frequent
  updates would only fill the log. Decided with `stream.isatty()`.
- **Progress never runs inside a timed call.** The runner reports a call only
  after its duration has been read, so writing a progress line cannot inflate
  a measurement.
"""

from __future__ import annotations

from time import monotonic
from typing import IO, Any

PLAIN_INTERVAL_SECONDS = 5.0
"""Seconds between whole lines written to a pipe, a file, or a log."""

TERMINAL_INTERVAL_SECONDS = 0.2
"""Seconds between in-place updates written to a terminal."""


class Progress:
    """Writes what a run is doing to a stream.

    Attributes:
        stream: Stream progress is written to.
        rewrite: Whether one line is rewritten in place instead of appending.
        interval: Seconds between updates.
        enabled: Whether anything is written at all.
    """

    def __init__(
        self,
        stream: IO[str],
        *,
        rewrite: bool,
        interval: float,
        enabled: bool = True,
    ) -> None:
        """Create a progress reporter.

        Args:
            stream: Stream to write to. Standard error keeps the table and the
                JSON file clean.
            rewrite: Rewrite one line in place instead of appending lines.
            interval: Seconds between updates.
            enabled: Write nothing when false.
        """

        self.stream = stream
        self.rewrite = rewrite
        self.interval = interval
        self.enabled = enabled
        self._label = ""
        self._prefix = ""
        self._started = 0.0
        self._last_write = 0.0
        self._line_width = 0

    def start_variant(self, position: int, total: int, label: str) -> None:
        """Announce the variant about to be measured.

        Args:
            position: One-based position of this variant in the run.
            total: Number of variants in the run.
            label: Target, layer, and case name.
        """

        self._label = label
        self._prefix = f"[{position}/{total}] {label}"
        self._started = monotonic()
        self._last_write = 0.0
        self._line_width = 0
        if self.enabled and not self.rewrite:
            self._write_line(f"{self._prefix} started")

    def call_done(self, finished: int, total: int) -> None:
        """Report that one call finished and its duration was recorded.

        Args:
            finished: Calls finished so far, warmup included.
            total: Calls this variant will make.
        """

        if not self.enabled:
            return
        now = monotonic()
        if finished < total and now - self._last_write < self.interval:
            return
        self._last_write = now
        elapsed = now - self._started
        line = f"{self._prefix}  {finished}/{total} calls  {elapsed:.1f}s"
        remaining = self._remaining(finished, total, elapsed)
        if remaining is not None:
            line = f"{line}  about {remaining} left"
        if self.rewrite:
            self._rewrite_line(line)
        else:
            self._write_line(line)

    def finish_variant(self, samples: int, elapsed_seconds: float, p50_ms: float) -> None:
        """Close a variant with what it measured, so the scrollback keeps it.

        Args:
            samples: Measured calls that returned.
            elapsed_seconds: Wall time the measured loop took.
            p50_ms: Median call latency in milliseconds.
        """

        if not self.enabled:
            return
        self._clear_line()
        self._write_line(
            f"{self._prefix}  {samples} calls in {elapsed_seconds:.1f}s  p50 {p50_ms:.3f} ms"
        )

    def skip_target(self, name: str, reason: str) -> None:
        """Report a target that cannot run, and why.

        Args:
            name: Target name.
            reason: Why it cannot run.
        """

        if not self.enabled:
            return
        self._clear_line()
        self._write_line(f"-- skipped {name}: {reason}")

    def close(self) -> None:
        """Leave the stream on a clean line."""

        if not self.enabled:
            return
        self._clear_line()
        self.stream.flush()

    def _remaining(self, finished: int, total: int, elapsed: float) -> str | None:
        """Return a rough estimate of the time left, when one is meaningful."""

        if finished <= 0 or finished >= total or elapsed <= 0:
            return None
        seconds = elapsed / finished * (total - finished)
        if seconds < 1:
            return None
        if seconds < 90:
            return f"{seconds:.0f}s"
        return f"{seconds / 60:.0f}m"

    def _rewrite_line(self, line: str) -> None:
        """Replace the current line in place, erasing whatever was longer."""

        padding = " " * max(self._line_width - len(line), 0)
        self.stream.write(f"\r{line}{padding}")
        self.stream.flush()
        self._line_width = len(line)

    def _write_line(self, line: str) -> None:
        """Append one whole line."""

        self.stream.write(f"{line}\n")
        self.stream.flush()
        self._line_width = 0

    def _clear_line(self) -> None:
        """Erase an in-place line so the next output starts clean."""

        if self.rewrite and self._line_width:
            self.stream.write(f"\r{' ' * self._line_width}\r")
            self._line_width = 0


def for_stream(stream: Any, *, quiet: bool = False) -> Progress:
    """Build the reporter that suits a stream.

    Args:
        stream: Stream progress will be written to.
        quiet: Write nothing at all.

    Returns:
        A reporter that rewrites one line on a terminal and writes whole lines
        far apart anywhere else.
    """

    terminal = False
    try:
        terminal = bool(stream.isatty())
    except (AttributeError, ValueError):
        terminal = False
    return Progress(
        stream,
        rewrite=terminal,
        interval=TERMINAL_INTERVAL_SECONDS if terminal else PLAIN_INTERVAL_SECONDS,
        enabled=not quiet,
    )
