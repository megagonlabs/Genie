"""Stores the benchmarks measure.

A target is one store plus the two functions that open and dispose of it. Every
measured variant gets a fresh instance, so nothing one variant writes can move
the next variant's numbers.

`skip_reason` is part of the shape from the start. The server-backed adapters
need a running server, and a target that cannot run reports a reason instead of
a number rather than being folded into a pass.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genie import InMemoryBackend, SQLiteBackend

SQLITE_DURABILITY = "full"
"""SQLite synchronous mode used here. The production-safe default."""


@dataclass(frozen=True, slots=True)
class BackendTarget:
    """One store the benchmarks can run against.

    Attributes:
        name: Short name used in reports and on the command line.
        storage: One-line description of what the target stores and where.
        opener: Returns a fresh backend and the function that disposes of it.
        skip_reason: Why the target cannot run, or `None` when it can.
    """

    name: str
    storage: str
    opener: Callable[[], tuple[Any, Callable[[], None]]]
    skip_reason: str | None = None

    @property
    def available(self) -> bool:
        """Whether the target can run in this environment."""

        return self.skip_reason is None


def _open_in_memory() -> tuple[Any, Callable[[], None]]:
    """Open a fresh in-memory backend that needs no disposal."""

    return InMemoryBackend(), lambda: None


def _open_sqlite() -> tuple[Any, Callable[[], None]]:
    """Open a fresh SQLite database on a file in a throwaway directory."""

    directory = tempfile.mkdtemp(prefix="genie-benchmark-")
    backend = SQLiteBackend(Path(directory) / "memory.db", durability=SQLITE_DURABILITY)

    def dispose() -> None:
        backend.close()
        shutil.rmtree(directory, ignore_errors=True)

    return backend, dispose


def default_targets() -> tuple[BackendTarget, ...]:
    """Return the targets a default run measures.

    Returns:
        The two targets that need no server and no extra package.
    """

    return (
        BackendTarget(
            name="in_memory",
            storage="InMemoryBackend, one process, nothing persisted",
            opener=_open_in_memory,
        ),
        BackendTarget(
            name="sqlite",
            storage=f"SQLiteBackend on a file in a throwaway directory, durability={SQLITE_DURABILITY!r}",
            opener=_open_sqlite,
        ),
    )
