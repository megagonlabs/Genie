"""Run settings that all benchmark cases share.

- **Purpose:** the settings set the duration of a run and the data that it
  uses. You can compare 2 reports only if their settings are the same. So each
  report records the values.
- **`workload`:** this is the only switch that changes the structure of a run
  and not only its size. It replaces the single-operation cases with the mixed
  read/write scenario. That scenario needs more than one worker, and its
  duration comes from `blocks`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Final

LAYERS: Final = ("backend", "memory", "core")
"""The layers that the harness can measure, from the raw store call to the
core-only helpers."""

BACKEND: Final = "backend"
"""One call on a backend capability protocol. No policy, no hooks, no codec."""

MEMORY: Final = "memory"
"""One call on the public `Memory` or `MemoryHandle` API."""

CORE: Final = "core"
"""One core-library helper alone, with no backend behind it."""

SINGLE_CALLER: Final = 1
"""The worker level that each run measures. It is the denominator of
`speedup`."""


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    """The numbers that describe one benchmark run.

    Attributes:
        iterations: The measured calls for each variant.
        warmup: The calls that the runner makes and discards before the
            measurement starts.
        blocks: The leaf blocks in the corpus of each variant.
        sections: The blocks in the chain between the corpus root and its
            leaves. The chain sets the tree depth, so `ancestors` and
            `contains` do real work.
        value_size: The padding characters that the generator adds to each
            value.
        links_per_block: The outgoing links on the corpus hub block.
        search_limit: The `limit` of each search. A backend search returns
            at most this number of candidates. A `Memory` search returns at
            most this number of visible results.
        seed: The seed for the corpus generator. The same seed always writes
            the same blocks.
        workers: The caller counts to measure, smallest first. Each level runs
            the same case with that number of threads. The threads share one
            `Memory` and one backend. `iterations` and `warmup` count for each
            worker, so a variant with 4 workers makes 4 times as many calls.
            The run always measures level 1, because `speedup` needs a
            single-caller denominator.
        workload: If `True`, the run measures the mixed read/write scenario
            instead of the single-operation cases. The scenario needs more than
            one worker, because one worker cannot read and write at the same
            time. It uses `blocks` as the size of one full write sweep. This
            sweep sets the duration of its measured window, so `iterations`
            does not apply to it.
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
    workload: bool = False

    def __post_init__(self) -> None:
        """Refuse counts that cannot describe a run, and normalize the levels.

        The method sorts the worker levels, removes duplicates, and always adds
        level 1. So 2 runs that ask for the same levels in a different order
        report the same rows.

        Raises:
            TypeError: If a count field is not an integer, `workers` is not a
                sequence of integers, or `workload` is not a boolean.
            ValueError: If a count is out of range, a worker level is below 1,
                the corpus cannot hold the requested sections or links, or the
                other settings cannot describe the requested mixed run.
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
        if not isinstance(self.workload, bool):
            raise TypeError("workload must be a boolean.")
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
        if self.workload:
            self._check_workload()

    def _check_workload(self) -> None:
        """Refuse a mixed run that the other settings cannot describe.

        Raises:
            ValueError: If no worker level is above one caller, or the corpus
                has fewer leaves than the largest writer count. In that case, a
                writer has no range to sweep.
        """

        # The import is in the method on purpose. The workload module imports
        # the case helpers, and those helpers read these settings. An import at
        # module scope would make an import cycle.
        from benchmarks.workload import split_roles

        if not self.workload_levels:
            raise ValueError("a mixed workload needs more than one worker; add --workers 4.")
        writers = max(split_roles(level).writers for level in self.workload_levels)
        if self.blocks < writers:
            raise ValueError(f"a mixed workload at {max(self.workload_levels)} workers needs at least {writers} blocks.")

    @property
    def total_calls(self) -> int:
        """The calls that one worker makes, with the warmup calls.

        This value is for a single-operation case. A mixed run counts its calls
        from the write sweep, so it does not use this value.
        """

        return self.warmup + self.iterations

    @property
    def measures_concurrency(self) -> bool:
        """`True` if the settings ask for a level above one caller."""

        return max(self.workers) > SINGLE_CALLER

    @property
    def workload_levels(self) -> tuple[int, ...]:
        """The worker levels that a mixed run measures, smallest first.

        Level 1 is not in the result. One worker cannot read and write at the
        same time. A mixed variant is the only type of variant that has no
        single-caller form.
        """

        return tuple(level for level in self.workers if level > SINGLE_CALLER)

    def as_dict(self) -> dict[str, Any]:
        """Return the settings as plain data for the JSON report."""

        data = asdict(self)
        data["workers"] = list(self.workers)
        return data


def _normalize_workers(workers: Any) -> tuple[int, ...]:
    """Sort the worker levels, remove duplicates, and add the single-caller level.

    Args:
        workers: The requested levels, in any order.

    Returns:
        The levels to measure, smallest first. The first level is always 1.

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
