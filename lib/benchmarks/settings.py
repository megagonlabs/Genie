"""Run settings shared by every benchmark case.

The settings decide how long a run takes and what data it runs against. Two
reports are comparable only when their settings match, so the values are
recorded in every report.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Final

LAYERS: Final = ("backend", "memory", "core")
"""Measurable layers, from the raw store call up to the core-only helpers."""

BACKEND: Final = "backend"
"""One call on a backend capability protocol. No policy, no hooks, no codec."""

MEMORY: Final = "memory"
"""One call on the public `Memory` or `MemoryHandle` API."""

CORE: Final = "core"
"""One core-library helper on its own, with no backend behind it."""

SINGLE_CALLER: Final = 1
"""Worker level every run measures. It is the denominator of `speedup`."""


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    """Numbers that describe one benchmark run.

    Attributes:
        iterations: Measured calls per variant.
        warmup: Calls made and thrown away before measuring starts.
        blocks: Leaf blocks in the corpus each variant runs against.
        sections: Blocks in the chain between the corpus root and its leaves.
            The chain sets tree depth, so `ancestors` and `contains` have real
            work to do.
        value_size: Padding characters added to each generated value.
        links_per_block: Outgoing links attached to the corpus hub block.
        search_limit: Candidate budget passed to search. Backends apply it
            before policy and scope filtering, so a search can return fewer
            blocks than this.
        seed: Seed for the corpus generator. The same seed always writes the
            same blocks.
        workers: Caller counts to measure, smallest first. Every level runs the
            same case with that many threads sharing one `Memory` and one
            backend. `iterations` and `warmup` are counted per worker, so a
            variant at four workers makes four times as many calls. Level 1 is
            always measured, because `speedup` needs a single-caller
            denominator.
    """

    iterations: int = 100
    warmup: int = 10
    blocks: int = 1000
    sections: int = 20
    value_size: int = 256
    links_per_block: int = 50
    search_limit: int = 20
    seed: int = 20260903
    workers: tuple[int, ...] = (SINGLE_CALLER,)

    def __post_init__(self) -> None:
        """Reject counts that cannot describe a run, and normalize the levels.

        Worker levels are sorted, de-duplicated, and always include level 1, so
        two runs that asked for the same levels in a different order report the
        same rows.

        Raises:
            TypeError: If any field is not an integer, or `workers` is not a
                sequence of integers.
            ValueError: If a count is out of range, a worker level is below 1,
                or the corpus cannot hold the requested sections or links.
        """

        for name in (
            "iterations",
            "warmup",
            "blocks",
            "sections",
            "value_size",
            "links_per_block",
            "search_limit",
            "seed",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer.")
        if self.iterations < 1:
            raise ValueError("iterations must be at least 1.")
        if self.warmup < 0:
            raise ValueError("warmup must be zero or more.")
        if self.blocks < 1:
            raise ValueError("blocks must be at least 1.")
        if self.sections < 1:
            raise ValueError("sections must be at least 1.")
        if self.value_size < 0:
            raise ValueError("value_size must be zero or more.")
        if self.links_per_block < 0:
            raise ValueError("links_per_block must be zero or more.")
        if self.search_limit < 0:
            raise ValueError("search_limit must be zero or more.")
        if self.sections > self.blocks:
            raise ValueError("sections must not be greater than blocks.")
        if self.links_per_block > self.blocks:
            raise ValueError("links_per_block must not be greater than blocks.")
        object.__setattr__(self, "workers", _normalize_workers(self.workers))

    @property
    def total_calls(self) -> int:
        """Calls one worker makes, warmup included."""

        return self.warmup + self.iterations

    @property
    def measures_concurrency(self) -> bool:
        """Whether any level above one caller was asked for."""

        return max(self.workers) > SINGLE_CALLER

    def as_dict(self) -> dict[str, Any]:
        """Return the settings as plain data for the JSON report."""

        data = asdict(self)
        data["workers"] = list(self.workers)
        return data


def _normalize_workers(workers: Any) -> tuple[int, ...]:
    """Sort worker levels, drop duplicates, and add the single-caller level.

    Args:
        workers: Levels asked for, in any order.

    Returns:
        The levels to measure, smallest first, always starting at 1.

    Raises:
        TypeError: If `workers` is not a sequence of integers.
        ValueError: If a level is below 1.
    """

    if isinstance(workers, (str, bytes)) or not isinstance(workers, Iterable):
        raise TypeError("workers must be a sequence of integers.")
    levels = set()
    for level in workers:
        if isinstance(level, bool) or not isinstance(level, int):
            raise TypeError("workers must be a sequence of integers.")
        if level < 1:
            raise ValueError("every worker level must be at least 1.")
        levels.add(level)
    levels.add(SINGLE_CALLER)
    return tuple(sorted(levels))
