"""The mixed read/write scenario, and the worker roles it splits into.

Every other benchmark case measures one operation with the store otherwise
idle. This module describes the one scenario that does not: readers and writers
working on the same blocks at the same time, so the report can say how much
they slow each other down.

The scenario is fixed. It has no flags of its own beyond `--workload`, and the
numbers it does need come from settings that already exist:

- **Readers and writers are split 3 to 1** from the worker count. Every worker
  keeps its role for the whole measured window, so read latency comes only
  from threads that did nothing but read.
- **Readers walk the leaf list forward from a spread offset** and wrap around,
  looping until they are told to stop.
- **Writers divide the leaf list into contiguous ranges** and walk their own
  range once, in order. So the corpus is rewritten exactly once however many
  writers there are, and two writers never touch the same block.
- **The window lasts exactly one sweep.** It ends when the last writer
  finishes its range, which makes the run length a property of `--blocks`
  rather than a duration someone picked, and guarantees that every block a
  reader can land on is rewritten during the window.

Both streams call the public `Memory` API. A raw backend write cannot join this
scenario, because `update_block` needs an `expected_version` and a benchmark
cannot know that version in advance once a second worker writes the same block.
`Memory.set` resolves the version itself.

This module holds the scenario only. The barrier, the stop signal, and the
measured window live with the rest of the timing code in `runner`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from benchmarks.cases import CaseContext, PreparedCase
from benchmarks.dataset import payload
from genie import MemoryConflict

READ_PER_WRITE = 3
"""Readers per writer. Divides cleanly into the common worker counts: 4 workers
give 3 readers and 1 writer, 8 give 6 and 2."""

MINIMUM_WORKERS = 2
"""Workers a mixed run needs. One worker cannot read and write at once."""

STREAM_READ = "read"
"""Name of the reading stream in reports."""

STREAM_WRITE = "write"
"""Name of the writing stream in reports."""

STREAMS = (STREAM_READ, STREAM_WRITE)
"""Both streams, in report order."""

WARMUP_BLOCK_PREFIX = "bench_workload_warmup"
"""Id prefix of the throwaway block a writer warms up on."""

WARMUP_BLOCK_TYPE = "bench.workload"
"""Block type of that throwaway block."""


@dataclass(frozen=True, slots=True)
class Roles:
    """How one worker count splits into readers and writers.

    Readers take the low worker indexes and writers the high ones, so a
    worker's role and its position inside its own stream can both be read off
    its index.

    Attributes:
        readers: Workers that do nothing but read.
        writers: Workers that do nothing but write.
    """

    readers: int
    writers: int

    @property
    def workers(self) -> int:
        """Workers in both streams together."""

        return self.readers + self.writers

    def stream_of(self, worker_index: int) -> str:
        """Return the stream one worker belongs to.

        Args:
            worker_index: Zero-based worker index within the variant.

        Returns:
            `STREAM_READ` or `STREAM_WRITE`.

        Raises:
            ValueError: If the index is outside this split.
        """

        self._check(worker_index)
        return STREAM_READ if worker_index < self.readers else STREAM_WRITE

    def position_of(self, worker_index: int) -> int:
        """Return a worker's zero-based position inside its own stream.

        Reader 0 is the first reader and writer 0 is the first writer, whatever
        their worker indexes are.

        Args:
            worker_index: Zero-based worker index within the variant.

        Returns:
            The position inside that worker's stream.

        Raises:
            ValueError: If the index is outside this split.
        """

        self._check(worker_index)
        if worker_index < self.readers:
            return worker_index
        return worker_index - self.readers

    def _check(self, worker_index: int) -> None:
        """Reject a worker index this split does not cover."""

        if not 0 <= worker_index < self.workers:
            raise ValueError(f"worker_index must be between 0 and {self.workers - 1}.")


def split_roles(workers: int) -> Roles:
    """Split a worker count into readers and writers.

    The writer count is rounded to the nearest whole worker, so a count that
    does not divide by four still lands close to `READ_PER_WRITE`. There is
    always at least one worker in each stream, which means a small count drifts
    away from the target ratio: two workers give one reader and one writer. The
    report prints the share it actually achieved for that reason.

    Args:
        workers: Workers the variant will run.

    Returns:
        The split.

    Raises:
        ValueError: If `workers` is below `MINIMUM_WORKERS`.
    """

    if workers < MINIMUM_WORKERS:
        raise ValueError(f"a mixed workload needs at least {MINIMUM_WORKERS} workers.")
    writers = max(1, (workers + READ_PER_WRITE - 1) // (READ_PER_WRITE + 1))
    writers = min(writers, workers - 1)
    return Roles(readers=workers - writers, writers=writers)


def read_offset(position: int, readers: int, blocks: int) -> int:
    """Return the leaf a reader starts its walk at.

    Readers are spaced evenly over the leaf list. The offset only stops several
    readers starting on the same page: a reader wraps around and covers the
    whole corpus many times over a full sweep, so where it began stops
    mattering after the first pass.

    Args:
        position: Zero-based position inside the reading stream.
        readers: Readers in the variant.
        blocks: Leaves in the corpus.

    Returns:
        The starting index into the leaf list.

    Raises:
        ValueError: If the counts cannot describe a reader.
    """

    if readers < 1:
        raise ValueError("readers must be at least 1.")
    if not 0 <= position < readers:
        raise ValueError(f"position must be between 0 and {readers - 1}.")
    if blocks < 1:
        raise ValueError("blocks must be at least 1.")
    return position * blocks // readers


def write_range(position: int, writers: int, blocks: int) -> tuple[int, int]:
    """Return the half-open slice of the leaf list one writer owns.

    The ranges are contiguous and cover every leaf exactly once, so one sweep
    rewrites the whole corpus no matter how the leaves divide among the
    writers. Because no leaf belongs to two writers, this scenario expects no
    write conflicts at all.

    Args:
        position: Zero-based position inside the writing stream.
        writers: Writers in the variant.
        blocks: Leaves in the corpus.

    Returns:
        The `start` and `stop` indexes into the leaf list.

    Raises:
        ValueError: If the counts cannot describe a writer, or there are fewer
            leaves than writers, which would leave a writer with nothing to do.
    """

    if writers < 1:
        raise ValueError("writers must be at least 1.")
    if not 0 <= position < writers:
        raise ValueError(f"position must be between 0 and {writers - 1}.")
    if blocks < writers:
        raise ValueError("blocks must not be fewer than writers.")
    return position * blocks // writers, (position + 1) * blocks // writers


@dataclass(frozen=True, slots=True)
class WorkloadWorker:
    """One prepared worker of a mixed variant.

    Attributes:
        stream: Stream this worker belongs to, used to group its numbers in the
            report.
        position: Zero-based position inside that stream.
        prepared: The call the runner times, in the same shape every benchmark
            case uses.
        measured_calls: Measured calls this worker will make, or `None` when it
            loops until it is told to stop. A writer knows its count, because
            it is the size of the range it owns. A reader does not, because the
            writers decide when the window ends.
    """

    stream: str
    position: int
    prepared: PreparedCase
    measured_calls: int | None


def prepare_worker(context: CaseContext, roles: Roles) -> WorkloadWorker:
    """Prepare the one worker named by `context.worker_index`.

    Preparation runs in the calling thread before the measured window opens, so
    nothing here is timed. Every payload a writer sends is built now, which
    keeps a timed call down to the `Memory` operation itself.

    Args:
        context: Case context naming the worker and holding the corpus.
        roles: How the variant's workers split into the two streams.

    Returns:
        The prepared worker.

    Raises:
        ValueError: If the context's worker index is outside `roles`, or the
            corpus is too small for the split.
    """

    position = roles.position_of(context.worker_index)
    if roles.stream_of(context.worker_index) == STREAM_READ:
        return _prepare_reader(context, roles, position)
    return _prepare_writer(context, roles, position)


def _prepare_reader(context: CaseContext, roles: Roles, position: int) -> WorkloadWorker:
    """Prepare a reader that walks the leaf list forward and wraps around."""

    ids = tuple(leaf.id for leaf in context.corpus.leaves)
    offset = read_offset(position, roles.readers, len(ids))
    memory = context.memory
    total = len(ids)
    return WorkloadWorker(
        stream=STREAM_READ,
        position=position,
        prepared=PreparedCase(
            call=lambda index: memory.get(ids[(offset + index) % total]),
            describe=_read_notes,
        ),
        measured_calls=None,
    )


def _prepare_writer(context: CaseContext, roles: Roles, position: int) -> WorkloadWorker:
    """Prepare a writer that rewrites its own range of the leaf list once.

    Warmup writes go to a throwaway block of the writer's own rather than to a
    corpus leaf. Otherwise the first `warmup` calls would eat into the range,
    and the sweep would no longer be exactly one pass over the corpus.
    """

    ids = tuple(leaf.id for leaf in context.corpus.leaves)
    start, stop = write_range(position, roles.writers, len(ids))
    sweep = ids[start:stop]
    memory = context.memory
    warmup = context.settings.warmup
    value_size = context.settings.value_size
    warmup_id = f"{WARMUP_BLOCK_PREFIX}_w{position:03d}"
    # A root of its own, so warming up cannot change what a traversal or a
    # scoped read inside the corpus would see.
    memory.put(
        type=WARMUP_BLOCK_TYPE,
        value=payload(0, value_size=value_size),
        metadata={"role": "workload_warmup", "position": position},
        id=warmup_id,
    )
    values = [payload(index, value_size=value_size) for index in range(warmup + len(sweep))]

    def call(index: int) -> Any:
        """Write the warmup block first, then walk the owned range in order."""

        if index < warmup:
            return memory.set(warmup_id, values[index])
        return memory.set(sweep[index - warmup], values[index])

    return WorkloadWorker(
        stream=STREAM_WRITE,
        position=position,
        prepared=PreparedCase(
            call=call,
            describe=_write_notes,
            # Disjoint ranges mean a conflict should never happen here. Counting
            # it as a conflict rather than an error keeps it out of the exit
            # status while still showing it in its own report column, where a
            # non-zero value says the ranges are wrong or something outside the
            # run is writing the store.
            conflict_errors=(MemoryConflict,),
        ),
        measured_calls=len(sweep),
    )


def _read_notes(result: Any) -> Mapping[str, Any]:
    """Note whether the last read found its block."""

    return {"found": result is not None}


def _write_notes(result: Any) -> Mapping[str, Any]:
    """Note the version the last write left behind.

    A sweep writes each leaf once, so a corpus block ends at version `2`.
    """

    return {"version": getattr(result, "version", None)}
