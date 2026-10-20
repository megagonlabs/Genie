"""Live progress for a run that is in progress.

A benchmark run can stay in one case for one minute. Without output, this looks
like a hang. So the reporter shows that the run moves: the completed calls, the
seconds used, and an approximate estimate of the time that remains.

2 rules control this module:

- **The output style agrees with the stream.** A terminal gets one line that
  the reporter writes again in the same place, more than one time each second.
  A pipe, a file, or a CI log gets full lines with long gaps between them.
  Nothing there can erase a line, and frequent updates only make the log
  longer. `stream.isatty()` selects the style.
- **Progress never runs in a timed call.** The runner reports a call only after
  it reads the duration of that call. So a progress line cannot make a
  measurement larger.
"""

from __future__ import annotations

from time import monotonic
from typing import IO, Any

PLAIN_INTERVAL_SECONDS = 5.0
"""The seconds between full lines to a pipe, a file, or a log."""

TERMINAL_INTERVAL_SECONDS = 0.2
"""The seconds between updates in place on a terminal."""


class Progress:
    """Writes the current work of a run to a stream.

    Attributes:
        stream: The stream that gets the progress.
        rewrite: If `True`, the reporter writes one line again in the same
            place. If `False`, it adds new lines.
        interval: The seconds between updates.
        enabled: If `False`, the reporter writes nothing.
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
            stream: The stream to write to. With standard error, the table and
                the JSON file stay clean.
            rewrite: If `True`, write one line again in the same place, and do
                not add new lines.
            interval: The seconds between updates.
            enabled: If `False`, write nothing.
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
        """Announce the variant that the runner measures next.

        Args:
            position: The one-based position of this variant in the run.
            total: The number of variants in the run.
            label: The target, layer, and case name.
        """

        self._label = label
        self._prefix = f"[{position}/{total}] {label}"
        self._started = monotonic()
        self._last_write = 0.0
        self._line_width = 0
        if not self.enabled:
            return
        # Setup writes all of the corpus before the runner times the first call.
        # On a server target, setup is most of the wall time of the variant.
        # The announcement comes now, so the slowest part of a run always has
        # output.
        if self.rewrite:
            self._rewrite_line(f"{self._prefix}  preparing")
        else:
            self._write_line(f"{self._prefix} preparing")

    def call_done(self, finished: int, total: int) -> None:
        """Report that one call is complete and the runner recorded its duration.

        Args:
            finished: The calls that are complete at this time, with the warmup
                calls.
            total: The calls that this variant makes.
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
        """Close a variant with its results, so the scrollback keeps them.

        Args:
            samples: The measured calls that returned.
            elapsed_seconds: The wall time of the measured loop.
            p50_ms: The median call latency in milliseconds.
        """

        if not self.enabled:
            return
        self._clear_line()
        self._write_line(
            f"{self._prefix}  {samples} calls in {elapsed_seconds:.1f}s  p50 {p50_ms:.3f} ms"
        )

    def skip_target(self, name: str, reason: str) -> None:
        """Report a target that cannot run, and the reason.

        Args:
            name: The target name.
            reason: The reason that the target cannot run.
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
        """Return an approximate estimate of the time that remains, if the estimate has a meaning."""

        if finished <= 0 or finished >= total or elapsed <= 0:
            return None
        seconds = elapsed / finished * (total - finished)
        if seconds < 1:
            return None
        if seconds < 90:
            return f"{seconds:.0f}s"
        return f"{seconds / 60:.0f}m"

    def _rewrite_line(self, line: str) -> None:
        """Replace the current line in place, and erase the rest of a longer line."""

        padding = " " * max(self._line_width - len(line), 0)
        self.stream.write(f"\r{line}{padding}")
        self.stream.flush()
        self._line_width = len(line)

    def _write_line(self, line: str) -> None:
        """Add one full line."""

        self.stream.write(f"{line}\n")
        self.stream.flush()
        self._line_width = 0

    def _clear_line(self) -> None:
        """Erase a line in place, so the next output starts on a clean line."""

        if self.rewrite and self._line_width:
            self.stream.write(f"\r{' ' * self._line_width}\r")
            self._line_width = 0


def for_stream(stream: Any, *, quiet: bool = False) -> Progress:
    """Build the correct reporter for a stream.

    Args:
        stream: The stream that gets the progress.
        quiet: If `True`, write nothing.

    Returns:
        A reporter. On a terminal, it writes one line again in the same place.
        On all other streams, it writes full lines with long gaps between them.
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
