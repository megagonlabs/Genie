"""The mixed read/write scenario, and the worker roles in it.

All other benchmark cases measure one operation, and the store does no other
work. This module describes the only scenario that is different. In it,
readers and writers use the same blocks at the same time. So the report can
show how much they make each other slower.

The scenario is fixed. It has no flags of its own, other than `--workload`. The
numbers that it needs come from settings that already exist:

- **The split is 3 readers to 1 writer,** from the worker count. Each worker
  keeps its role for all of the measured window. So the read latency comes only
  from threads that only read.
- **Readers go forward through the leaf list from a spread offset.** At the
  end of the list, a reader goes back to the start. Readers continue until
  they get the stop signal.
- **Writers divide the leaf list into contiguous ranges.** Each writer goes
  through its own range one time, in sequence. So, with any number of writers,
  the writers write the corpus again exactly one time. No 2 writers write the
  same block.
- **The window is exactly one sweep.** It ends when the last writer completes
  its range. So `--blocks` sets the duration of the run, not a selected
  duration. Also, the writers write again each block that a reader can read
  during the window.

Both streams call the public `Memory` API. A raw backend write cannot be part
of this scenario. `update_block` needs an `expected_version`. If a second
worker writes the same block, a benchmark cannot know that version before the
call. `Memory.set` gets the version itself.

This module contains only the scenario. The barrier, the stop signal, and the
measured window are in `runner`, with the other code that measures time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from benchmarks.cases import CaseContext, PreparedCase
from benchmarks.dataset import payload
from genie import MemoryConflict

READ_PER_WRITE = 3
"""The readers for each writer. The common worker counts divide by it with no
remainder: 4 workers give 3 readers and 1 writer, and 8 workers give 6 readers
and 2 writers."""

MINIMUM_WORKERS = 2
"""The workers that a mixed run needs. One worker cannot read and write at the
same time."""

STREAM_READ = "read"
"""The name of the read stream in reports."""

STREAM_WRITE = "write"
"""The name of the write stream in reports."""

STREAMS = (STREAM_READ, STREAM_WRITE)
"""Both streams, in report order."""

WARMUP_BLOCK_PREFIX = "bench_workload_warmup"
"""The id prefix of the temporary block for the warmup of a writer."""

WARMUP_BLOCK_TYPE = "bench.workload"
"""The block type of that temporary block."""


@dataclass(frozen=True, slots=True)
class Roles:
    """How one worker count divides into readers and writers.

    Readers get the low worker indexes, and writers get the high indexes. So
    the index of a worker gives its role and its position in its own stream.

    Attributes:
        readers: The workers that only read.
        writers: The workers that only write.
    """

    readers: int
    writers: int

    @property
    def workers(self) -> int:
        """The workers in both streams together."""

        return self.readers + self.writers

    def stream_of(self, worker_index: int) -> str:
        """Return the stream of one worker.

        Args:
            worker_index: The zero-based worker index in the variant.

        Returns:
            `STREAM_READ` or `STREAM_WRITE`.

        Raises:
            ValueError: If the index is outside this split.
        """

        self._check(worker_index)
        return STREAM_READ if worker_index < self.readers else STREAM_WRITE

    def position_of(self, worker_index: int) -> int:
        """Return the zero-based position of a worker in its own stream.

        Reader 0 is the first reader, and writer 0 is the first writer. Their
        worker indexes have no effect on this.

        Args:
            worker_index: The zero-based worker index in the variant.

        Returns:
            The position in the stream of that worker.

        Raises:
            ValueError: If the index is outside this split.
        """

        self._check(worker_index)
        if worker_index < self.readers:
            return worker_index
        return worker_index - self.readers

    def _check(self, worker_index: int) -> None:
        """Refuse a worker index that is not in this split."""

        if not 0 <= worker_index < self.workers:
            raise ValueError(f"worker_index must be between 0 and {self.workers - 1}.")


def split_roles(workers: int) -> Roles:
    """Divide a worker count into readers and writers.

    - **Writer count:** the function rounds the writer count to the nearest whole
      worker. So a count that 4 does not divide still gives a ratio near
      `READ_PER_WRITE`.
    - **Small counts:** each stream always has a minimum of one worker. So a
      small count gives a ratio that is different from the target ratio. For
      example, 2 workers give 1 reader and 1 writer. For this reason, the
      report prints the measured share.

    Args:
        workers: The workers that the variant runs.

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
    """Return the leaf where a reader starts.

    - **Start positions:** the readers start at equal distances along the leaf
      list.
    - **Purpose:** the offset only prevents many readers from a start on the
      same page. At the end of the list, a reader goes back to the start. It
      reads all of the corpus many times during a full sweep. So, after the
      first pass, its start position has no effect.

    Args:
        position: The zero-based position in the read stream.
        readers: The readers in the variant.
        blocks: The leaves in the corpus.

    Returns:
        The start index in the leaf list.

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
    """Return the half-open slice of the leaf list that one writer owns.

    - **Full cover:** the ranges are contiguous, and they contain each leaf
      exactly one time. So one sweep writes all of the corpus again, with any
      division of the leaves among the writers.
    - **No conflicts:** no leaf belongs to 2 writers. So this scenario expects
      no write conflicts.

    Args:
        position: The zero-based position in the write stream.
        writers: The writers in the variant.
        blocks: The leaves in the corpus.

    Returns:
        The `start` and `stop` indexes in the leaf list.

    Raises:
        ValueError: If the counts cannot describe a writer, or if there are
            fewer leaves than writers. In that case, a writer has no work.
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
        stream: The stream of this worker. The report uses it to group the
            numbers of the worker.
        position: The zero-based position in that stream.
        prepared: The call that the runner times, in the same structure as all
            benchmark cases.
        measured_calls: The measured calls that this worker makes, or `None`
            if it continues until it gets the stop signal. A writer knows its
            count, because the count is the size of its range. A reader does
            not know its count, because the writers decide when the window
            ends.
    """

    stream: str
    position: int
    prepared: PreparedCase
    measured_calls: int | None


def prepare_worker(context: CaseContext, roles: Roles) -> WorkloadWorker:
    """Prepare the worker that `context.worker_index` names.

    Preparation runs in the calling thread before the measured window opens.
    So the runner times nothing here. The function builds all payloads of a
    writer now, so a timed call contains only the `Memory` operation.

    Args:
        context: The case context. It names the worker and contains the
            corpus.
        roles: How the workers of the variant divide into the 2 streams.

    Returns:
        The prepared worker.

    Raises:
        ValueError: If the worker index of the context is outside `roles`, or
            the corpus is too small for the split.
    """

    position = roles.position_of(context.worker_index)
    if roles.stream_of(context.worker_index) == STREAM_READ:
        return _prepare_reader(context, roles, position)
    return _prepare_writer(context, roles, position)


def _prepare_reader(context: CaseContext, roles: Roles, position: int) -> WorkloadWorker:
    """Prepare a reader that goes forward through the leaf list, and goes back to the start at the end."""

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
    """Prepare a writer that writes its own range of the leaf list again, one time.

    Warmup writes go to a temporary block that only this writer uses, not to a
    corpus leaf. If not, the first `warmup` calls would use a part of the
    range, and the sweep would not be exactly one pass over the corpus.
    """

    ids = tuple(leaf.id for leaf in context.corpus.leaves)
    start, stop = write_range(position, roles.writers, len(ids))
    sweep = ids[start:stop]
    memory = context.memory
    warmup = context.settings.warmup
    value_size = context.settings.value_size
    warmup_id = f"{WARMUP_BLOCK_PREFIX}_w{position:03d}"
    # The block is a separate root. So the warmup cannot change the result of a
    # traversal or a scoped read in the corpus.
    memory.put(
        type=WARMUP_BLOCK_TYPE,
        value=payload(0, value_size=value_size),
        metadata={"role": "workload_warmup", "position": position},
        id=warmup_id,
    )
    values = [payload(index, value_size=value_size) for index in range(warmup + len(sweep))]

    def call(index: int) -> Any:
        """Write the warmup block first, then go through the owned range in sequence."""

        if index < warmup:
            return memory.set(warmup_id, values[index])
        return memory.set(sweep[index - warmup], values[index])

    return WorkloadWorker(
        stream=STREAM_WRITE,
        position=position,
        prepared=PreparedCase(
            call=call,
            describe=_write_notes,
            # The ranges are separate, so a conflict does not occur here in a
            # correct run. The runner counts it as a conflict, not as an error.
            # So it has no effect on the exit status, but it shows in its own
            # report column. A value that is not 0 means that the ranges are
            # wrong, or that a process outside the run writes to the store.
            conflict_errors=(MemoryConflict,),
        ),
        measured_calls=len(sweep),
    )


def _read_notes(result: Any) -> Mapping[str, Any]:
    """Record if the last read found its block."""

    return {"found": result is not None}


def _write_notes(result: Any) -> Mapping[str, Any]:
    """Record the version that the last write gave.

    A sweep writes each leaf one time, so a corpus block ends at version `2`.
    """

    return {"version": getattr(result, "version", None)}
