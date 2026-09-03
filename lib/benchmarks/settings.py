"""Run settings shared by every benchmark case.

The settings decide how long a run takes and what data it runs against. Two
reports are comparable only when their settings match, so the values are
recorded in every report.
"""

from __future__ import annotations

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
    """

    iterations: int = 100
    warmup: int = 10
    blocks: int = 1000
    sections: int = 20
    value_size: int = 256
    links_per_block: int = 50
    search_limit: int = 20
    seed: int = 20260903

    def __post_init__(self) -> None:
        """Reject counts that cannot describe a run.

        Raises:
            TypeError: If any field is not an integer.
            ValueError: If a count is out of range, or the corpus cannot hold
                the requested sections or links.
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

    @property
    def total_calls(self) -> int:
        """Calls one variant makes, warmup included."""

        return self.warmup + self.iterations

    def as_dict(self) -> dict[str, Any]:
        """Return the settings as plain data for the JSON report."""

        return asdict(self)
