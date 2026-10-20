"""Runs the benchmark cases and returns a report.

This module never writes to a stream. So a test can do a full run without a
terminal. `report` and `run` print the output.

The steps of one measured variant:

1. **Setup.** The runner opens a new backend and writes the corpus. Then the
   case prepares its own targets. The report gives the wall time as
   `setup_seconds`. No latency number includes it.
2. **Warmup.** The runner makes `settings.warmup` calls and discards them. So
   the first-call cost of a connection, a cache, or a query plan stays out of
   the percentiles.
3. **Measurement.** The runner times `settings.iterations` calls, one at a
   time, with `time.perf_counter_ns`. It counts a call that raises an
   exception, but does not time it.
4. **Disposal.** The target disposes of the store. For `sqlite`, it closes the
   backend and removes its files. For a server target, it empties the store and
   closes the backend. So the writes of one variant cannot change the numbers
   of the next variant.

With more than one worker, the same 4 steps run, with 3 differences:

- **Setup still runs in the calling thread.** The preparation runs one time
  for each worker, for one worker at a time. So preparation can never race,
  and the runner never times it.
- **A barrier starts the measured window for all workers at the same
  instant.** Warmup occurs before the barrier. So a slow first call cannot
  occur in the measurement of a different worker.
- **The calling thread polls the progress. The workers do not push it.** Each
  worker writes its count of completed calls into its own slot, and the
  calling thread adds those slots. The measured loop of a worker takes no
  lock, so the progress report cannot change the throughput.

`settings.workload` replaces all of that with the mixed read/write scenario.
This scenario measures 3 passes for each target and worker level, not one
variant:

1. **Mixed.** Readers and writers run together. The window ends when the last
   writer completes its part of one sweep over the corpus.
2. **Reads alone.** Each reader makes exactly the same number of calls as in
   the mixed pass. No writer runs.
3. **Writes alone.** The writers sweep the corpus one more time. No reader
   runs.

The 3 passes run in that order, because pass 2 needs the call counts from pass
1. So one work item contains all 3 passes, and not one variant for each pass.
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Barrier, Event
from time import perf_counter, perf_counter_ns, sleep
from typing import Any

from benchmarks import dataset, environment
from benchmarks.cases import BenchmarkCase, CaseContext, PreparedCase, default_cases
from benchmarks.metrics import NANOSECONDS_PER_MILLISECOND, Summary, summarize
from benchmarks.progress import Progress
from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, SINGLE_CALLER, BenchmarkSettings
from benchmarks.targets import BackendTarget, default_targets
from benchmarks.workload import (
    STREAM_READ,
    STREAM_WRITE,
    STREAMS,
    Roles,
    WorkloadWorker,
    prepare_worker,
    split_roles,
)
from genie import InMemoryBackend, Memory

REPORT_SCHEMA = 3
"""The version of the report structure. It increases when the JSON layout
changes."""

PASS_MIXED = "mixed"
"""The pass in which readers and writers run together. This is the real
measurement."""

PASS_READS_ALONE = "reads_alone"
"""The pass in which the readers make their mixed call counts again, with no
writer."""

PASS_WRITES_ALONE = "writes_alone"
"""The pass in which the writers sweep the corpus one time, with no reader."""

WORKLOAD_PASSES = (PASS_MIXED, PASS_READS_ALONE, PASS_WRITES_ALONE)
"""The 3 passes of one mixed variant, in their necessary order."""

WORKLOAD_PASS_NOTES = {
    PASS_MIXED: "readers and writers together; the window ends when the sweep ends",
    PASS_READS_ALONE: "the readers repeat their mixed call counts, with no writer",
    PASS_WRITES_ALONE: "the writers sweep the corpus once, with no reader",
}
"""What each pass does, for `--list` and for other output that explains a
run."""

CORE_SECTION = "core"
"""The section name for the cases that measure the core library with no
store."""

CORE_STORAGE = "core library only, no store behind it"
"""The one-line description of the core section."""

PRINCIPAL_ID = "benchmark"
"""The principal that owns all blocks, so no case measures a denial path."""

Results = dict[str, dict[str, dict[str, dict[int, dict[str, Any]]]]]
"""The measured summaries. The keys are the section, the case name, the layer,
and the worker count."""


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One variant to measure.

    Attributes:
        section: The report section of this variant.
        target: The store to open, or `None` for a core-only variant.
        case: The case to measure.
        layer: The layer of the case to measure.
        workers: The callers that run the case at the same time. They share
            one store and one `Memory`.
    """

    section: str
    target: BackendTarget | None
    case: BenchmarkCase
    layer: str
    workers: int = SINGLE_CALLER

    @property
    def label(self) -> str:
        """The progress label, with the section, layer, case, and worker count."""

        label = f"{self.section} {self.layer}: {self.case.name}"
        if self.workers > SINGLE_CALLER:
            return f"{label} x{self.workers}"
        return label


def plan_run(
    targets: Sequence[BackendTarget],
    cases: Sequence[BenchmarkCase],
    layers: Sequence[str],
    workers: Sequence[int] = (SINGLE_CALLER,),
) -> tuple[list[WorkItem], list[BackendTarget]]:
    """Find all variants that a run measures.

    - **Store-backed layers:** they run one time for each available target and
      for each worker level. The plan puts the worker levels of one case and
      layer next to each other. So the compared runs occur close together in
      time, and a slow change in the load of the machine cannot alone change
      `speedup`.
    - **`core` layer:** it runs one time, in its own section, with a single
      caller. A core helper has no store behind it. A repeat under each target
      gives only the same number again, and contention has no meaning there.

    Args:
        targets: The targets to measure.
        cases: The cases to measure.
        layers: The layers to measure.
        workers: The worker levels to measure, smallest first.

    Returns:
        The variants to measure, and the skipped targets.
    """

    store_layers = tuple(layer for layer in layers if layer in (BACKEND, MEMORY))
    levels = tuple(workers) or (SINGLE_CALLER,)
    items: list[WorkItem] = []
    skipped: list[BackendTarget] = []
    for target in targets:
        if not target.available:
            skipped.append(target)
            continue
        for case in cases:
            for layer in store_layers:
                if case.prepare_for(layer) is None:
                    continue
                for level in levels:
                    items.append(WorkItem(target.name, target, case, layer, level))
    if CORE in layers:
        for case in cases:
            if case.prepare_for(CORE) is not None:
                items.append(WorkItem(CORE_SECTION, None, case, CORE))
    return items, skipped


def run(
    *,
    settings: BenchmarkSettings | None = None,
    targets: Sequence[BackendTarget] | None = None,
    cases: Sequence[BenchmarkCase] | None = None,
    layers: Sequence[str] = LAYERS,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Measure all selected variants and return the report.

    - **`concurrent_only` cases:** the function measures them only if the
      settings ask for more than one worker. So a default run keeps its
      duration and its structure.
    - **`settings.workload`:** if it is set, the function ignores `cases` and
      `layers`, and measures the mixed read/write scenario.

    Args:
        settings: The run settings. The default is `BenchmarkSettings()`.
        targets: The targets to measure. The default is `default_targets()`.
        cases: The cases to measure. The default is `default_cases()`.
        layers: The layers to measure. The default is all layers.
        progress: The reporter. The function tells it about each call after it
            reads the duration of the call.

    Returns:
        The report as plain data, ready for the table renderer or the JSON
        writer.
    """

    run_settings = settings or BenchmarkSettings()
    run_targets = tuple(targets if targets is not None else default_targets())
    if run_settings.workload:
        return run_workload(settings=run_settings, targets=run_targets, progress=progress)
    run_cases = tuple(cases if cases is not None else default_cases())
    if not run_settings.measures_concurrency:
        run_cases = tuple(case for case in run_cases if not case.concurrent_only)
    items, skipped = plan_run(run_targets, run_cases, tuple(layers), run_settings.workers)
    started_at = datetime.now(UTC)
    started = perf_counter()
    results: Results = {}
    for position, item in enumerate(items, start=1):
        if progress is not None:
            progress.start_variant(position, len(items), item.label)
        summary = measure_variant(item, settings=run_settings, progress=progress)
        section = results.setdefault(item.section, {})
        section.setdefault(item.case.name, {}).setdefault(item.layer, {})[item.workers] = (
            summary.as_dict()
        )
    if progress is not None:
        progress.close()
    return {
        "schema": REPORT_SCHEMA,
        "started_at": started_at.isoformat(),
        "duration_seconds": perf_counter() - started,
        "environment": environment.describe(),
        "settings": run_settings.as_dict(),
        "layers": list(layers),
        "workers": list(run_settings.workers),
        "sections": _build_sections(run_targets, run_cases, tuple(layers), results, skipped),
        "errors": _total(results, "errors"),
        "conflicts": _total(results, "conflicts"),
    }


def _total(results: Results, field: str) -> int:
    """Add one counter over all measured variants.

    Args:
        results: The measured summaries.
        field: The summary field to add, for example `"errors"`.

    Returns:
        The total.
    """

    return sum(
        level_result[field]
        for section in results.values()
        for case_result in section.values()
        for layer_result in case_result.values()
        for level_result in layer_result.values()
    )


def measure_variant(
    item: WorkItem,
    *,
    settings: BenchmarkSettings,
    progress: Progress | None = None,
) -> Summary:
    """Prepare one variant, measure it, and dispose of its store.

    The function prepares each worker in this thread, one at a time. So setup
    can never race, and the runner never times it. Worker `0` does the setup
    that the workers share, for example the creation of the block that a
    contention case competes for.

    Args:
        item: The variant to measure.
        settings: The run settings.
        progress: The reporter. The function tells it about each call after it
            reads the duration of the call.

    Returns:
        The summary of the variant.
    """

    setup_started = perf_counter()
    context, dispose = build_context(item, settings)
    prepare = item.case.prepare_for(item.layer)
    if prepare is None:  # pragma: no cover - plan_run never selects such a variant
        dispose()
        raise ValueError(f"Case {item.case.name!r} has no {item.layer!r} layer.")
    try:
        prepared = [prepare(replace(context, worker_index=worker)) for worker in range(item.workers)]
        setup_seconds = perf_counter() - setup_started
        scale = _scale_notes(item, context)
        if item.workers > SINGLE_CALLER:
            summary = measure_concurrent(
                prepared,
                settings=settings,
                progress=progress,
                setup_seconds=setup_seconds,
                scale=scale,
            )
        else:
            summary = measure(
                prepared[0],
                settings=settings,
                progress=progress,
                setup_seconds=setup_seconds,
                scale=scale,
            )
    finally:
        dispose()
    return summary


def build_context(
    item: WorkItem,
    settings: BenchmarkSettings,
) -> tuple[CaseContext, Callable[[], None]]:
    """Open a new store, write the corpus, and wrap the store in a context.

    Args:
        item: The variant that the runner measures next.
        settings: The run settings.

    Returns:
        The case context, and the function that disposes of the store.
    """

    if item.target is None:
        backend: Any = InMemoryBackend()
        dispose: Callable[[], None] = lambda: None
    else:
        backend, dispose = item.target.opener()
    memory = Memory(backend=backend, principal=PRINCIPAL_ID)
    if item.case.needs_corpus:
        corpus = dataset.build(settings)
        if item.layer == BACKEND:
            dataset.populate_backend(
                backend,
                corpus,
                principal_id=PRINCIPAL_ID,
                policy=memory.default_policy,
                codecs=memory.codecs,
            )
        else:
            dataset.populate_memory(memory, corpus)
    else:
        # A core helper has no store behind it. So the function describes only
        # the smallest corpus that the case context needs, and writes nothing.
        corpus = dataset.build(replace(settings, blocks=1, sections=1, links_per_block=0))
    context = CaseContext(
        settings=settings,
        corpus=corpus,
        backend=backend,
        memory=memory,
        principal_id=PRINCIPAL_ID,
        worker_count=item.workers,
    )
    return context, dispose


def measure(
    prepared: PreparedCase,
    *,
    settings: BenchmarkSettings,
    progress: Progress | None = None,
    setup_seconds: float = 0.0,
    scale: dict[str, Any] | None = None,
) -> Summary:
    """Time one prepared case.

    Garbage collection runs one time before the loop and stays on, because a
    caller in production also pays that cost.

    Args:
        prepared: The case, ready for calls.
        settings: The run settings.
        progress: The reporter. The function tells it about each call after it
            reads the duration of the call.
        setup_seconds: The wall time that the preparation of this variant
            already used.
        scale: Notes that describe the data of the calls.

    Returns:
        The summary of the variant.
    """

    gc.collect()
    outcome = _Outcome(prepared.conflict_errors)
    total = settings.total_calls
    loop_started = perf_counter()
    measured_started = loop_started
    for index in range(total):
        measured = index >= settings.warmup
        if index == settings.warmup:
            measured_started = perf_counter()
        started = perf_counter_ns()
        try:
            result = prepared.call(index)
        except Exception as error:  # noqa: BLE001 - a failing call is a result, not a crash
            if measured:
                outcome.record_failure(error)
        else:
            elapsed = perf_counter_ns() - started
            if measured:
                outcome.record_success(elapsed, result)
        # The loop reports progress only after it reads the duration. So a
        # progress line can never make a measurement larger.
        if progress is not None:
            progress.call_done(index + 1, total)
    finished = perf_counter()
    summary = _summarize_outcomes(
        [outcome],
        prepared=prepared,
        workers=SINGLE_CALLER,
        wall_seconds=finished - measured_started,
        setup_seconds=setup_seconds,
        scale=scale,
    )
    if progress is not None:
        progress.finish_variant(summary.samples, finished - loop_started, summary.p50_ms)
    return summary


def measure_concurrent(
    prepared: Sequence[PreparedCase],
    *,
    settings: BenchmarkSettings,
    progress: Progress | None = None,
    setup_seconds: float = 0.0,
    scale: dict[str, Any] | None = None,
) -> Summary:
    """Time one prepared case, with more than one worker that calls it at the same time.

    All workers share the store and the `Memory` above it, as in an
    application. Each worker runs its own prepared case. So an ordinary case
    writes ids that no other worker uses, but a `contention` case sends all
    workers to one target.

    The measured window:

    - **Warmup occurs before a barrier.** After the warmup of all workers, the
      barrier releases them together and records the start of the window. So
      the first call of a worker cannot occur in the measurement of a
      different worker.
    - **The wall time ends at the last return of the last worker.**
      `throughput_per_second` is the number of returned calls divided by the
      wall time. This is the rate of all workers together. The report also
      gives `ops_per_second`, the rate of each caller.
    - **The latency percentiles come from one pool of the durations of all
      workers.** So one slow worker makes the tail wider. It does not stay
      hidden in an average.

    Args:
        prepared: One prepared case for each worker.
        settings: The run settings. `iterations` and `warmup` count for each
            worker.
        progress: The reporter. This thread polls it while the workers run.
        setup_seconds: The wall time that the preparation of this variant
            already used.
        scale: Notes that describe the data of the calls.

    Returns:
        The summary of the variant.

    Raises:
        RuntimeError: If a worker fails outside a measured call. Then the run
            has no correct number to report.
    """

    gc.collect()
    workers = len(prepared)
    per_worker = settings.total_calls
    finished_calls = [0] * workers
    outcomes = [_Outcome(case.conflict_errors) for case in prepared]
    window: list[float] = []
    warmed = Barrier(workers)
    measuring = Barrier(workers, action=lambda: window.append(perf_counter()))

    def work(worker: int) -> float:
        """Run the calls of one worker, and return the time of its last call."""

        call = prepared[worker].call
        outcome = outcomes[worker]
        try:
            for index in range(settings.warmup):
                # The worker discards the warmup results, also the failures. So
                # the first call of a connection or a query plan never goes
                # into the percentiles.
                with suppress(Exception):
                    call(index)
                finished_calls[worker] = index + 1
            warmed.wait()
            measuring.wait()
            for index in range(settings.warmup, per_worker):
                started = perf_counter_ns()
                try:
                    result = call(index)
                except Exception as error:  # noqa: BLE001 - a failing call is a result
                    outcome.record_failure(error)
                else:
                    outcome.record_success(perf_counter_ns() - started, result)
                # Each worker writes the count into its own slot. So the progress
                # report never takes a lock that the other workers could wait
                # for.
                finished_calls[worker] = index + 1
            return perf_counter()
        except BaseException:
            # If a worker stops before a barrier, the other workers wait and
            # never continue. So the worker breaks both barriers before it
            # stops.
            warmed.abort()
            measuring.abort()
            raise

    loop_started = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(work, worker) for worker in range(workers)]
        _follow_progress(progress, futures, finished_calls, per_worker * workers)
        try:
            last_return = max(future.result() for future in futures)
        except Exception as error:
            raise RuntimeError(f"A benchmark worker failed outside a measured call: {error}") from error
    summary = _summarize_outcomes(
        outcomes,
        prepared=prepared[0],
        workers=workers,
        wall_seconds=last_return - window[0] if window else 0.0,
        setup_seconds=setup_seconds,
        scale=scale,
    )
    if progress is not None:
        progress.finish_variant(summary.samples, perf_counter() - loop_started, summary.p50_ms)
    return summary


class _Outcome:
    """The results of the measured calls of one caller.

    This class keeps conflicts separate from errors. A `contention` case
    expects to lose races, and a lost race never causes a failed run.

    Attributes:
        conflict_errors: The exception types that count as conflicts.
        durations: The nanosecond durations of the calls that returned.
        errors: The calls that raised an unexpected exception.
        conflicts: The calls that lost a documented race.
        first_error: The type and message of the first unexpected failure.
        last_result: The last returned result, for the notes.
    """

    __slots__ = ("conflict_errors", "conflicts", "durations", "errors", "first_error", "last_result")

    def __init__(self, conflict_errors: tuple[type[BaseException], ...]) -> None:
        """Create an empty outcome.

        Args:
            conflict_errors: The exception types that count as conflicts.
        """

        self.conflict_errors = conflict_errors
        self.durations: list[int] = []
        self.errors = 0
        self.conflicts = 0
        self.first_error: str | None = None
        self.last_result: Any = None

    def record_success(self, duration_ns: int, result: Any) -> None:
        """Keep a call that returned.

        Args:
            duration_ns: The duration of the call.
            result: The return value of the call.
        """

        self.durations.append(duration_ns)
        self.last_result = result

    def record_failure(self, error: Exception) -> None:
        """Count a call that raised an exception, as a conflict or as an error.

        In both cases, the method discards the duration. So a call that did not
        complete its work cannot go into the latency percentiles.

        Args:
            error: The exception that the call raised.
        """

        if self.conflict_errors and isinstance(error, self.conflict_errors):
            self.conflicts += 1
            return
        self.errors += 1
        if self.first_error is None:
            self.first_error = f"{type(error).__name__}: {error}"


def _summarize_outcomes(
    outcomes: Sequence[_Outcome],
    *,
    prepared: PreparedCase,
    workers: int,
    wall_seconds: float,
    setup_seconds: float,
    scale: dict[str, Any] | None,
) -> Summary:
    """Merge the outcomes of all workers into one summary.

    Args:
        outcomes: One outcome for each worker.
        prepared: The prepared case. The function reads its `describe`
            function.
        workers: The callers that ran at the same time.
        wall_seconds: The wall time of the measured window.
        setup_seconds: The wall time to prepare the variant.
        scale: Notes that describe the data of the calls.

    Returns:
        The summary of the variant.
    """

    durations = [duration for outcome in outcomes for duration in outcome.durations]
    first_error = next((outcome.first_error for outcome in outcomes if outcome.first_error), None)
    last_result = next(
        (outcome.last_result for outcome in outcomes if outcome.last_result is not None), None
    )
    notes: dict[str, Any] = dict(scale or {})
    if prepared.describe is not None and last_result is not None:
        notes.update(prepared.describe(last_result))
    return summarize(
        durations,
        errors=sum(outcome.errors for outcome in outcomes),
        conflicts=sum(outcome.conflicts for outcome in outcomes),
        first_error=first_error,
        workers=workers,
        wall_seconds=wall_seconds,
        setup_seconds=setup_seconds,
        notes=notes,
    )


def _follow_progress(
    progress: Progress | None,
    futures: Sequence[Future[float]],
    finished_calls: Sequence[int],
    total: int,
    slots: Sequence[int] | None = None,
) -> None:
    """Report the completed work of the workers until all workers stop.

    The workers never call the reporter. This thread reads their counters. So
    the measured loop of a worker takes no lock, and the progress report cannot
    change the throughput.

    Args:
        progress: The reporter, or `None` to wait without output.
        futures: One future for each worker.
        finished_calls: One counter for each worker. Only that worker writes
            it.
        total: The calls that all of the variant makes, with the warmup calls.
        slots: The counters to add, or `None` for all counters. A mixed pass
            gives only its writers. The writers have a known call count, and
            they decide when the pass ends.
    """

    if progress is None:
        return
    pending = list(futures)
    while pending:
        _, not_done = wait(pending, timeout=progress.interval)
        counted = finished_calls if slots is None else [finished_calls[slot] for slot in slots]
        progress.call_done(sum(counted), total)
        pending = list(not_done)


def _scale_notes(item: WorkItem, context: CaseContext) -> dict[str, Any]:
    """Describe the data of a variant."""

    if not item.case.needs_corpus:
        return {}
    return {
        "corpus_blocks": context.corpus.block_count,
        "corpus_depth": context.corpus.depth,
    }


# --------------------------------------------------------------------------
# mixed read/write workload
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkloadItem:
    """One target and one worker level of the mixed scenario.

    Attributes:
        section: The report section. It is the target name.
        target: The store to measure. Each pass opens its own instance.
        workers: The callers that the variant runs.
        roles: How those callers divide into readers and writers.
    """

    section: str
    target: BackendTarget
    workers: int
    roles: Roles

    def label(self, pass_name: str) -> str:
        """Return the progress label for one pass of this variant.

        Args:
            pass_name: One of `WORKLOAD_PASSES`.

        Returns:
            A label with the target, the split, the worker count, and the pass.
        """

        split = f"{self.roles.readers}r/{self.roles.writers}w"
        return f"{self.section} workload {split} x{self.workers}: {pass_name}"


def plan_workload(
    targets: Sequence[BackendTarget],
    levels: Sequence[int],
) -> tuple[list[WorkloadItem], list[BackendTarget]]:
    """Find all mixed variants that a run measures.

    Args:
        targets: The targets to measure.
        levels: The worker levels to measure, smallest first. Level 1 is not
            one of them, because one worker cannot read and write at the same
            time.

    Returns:
        The variants to measure, and the skipped targets.
    """

    items: list[WorkloadItem] = []
    skipped: list[BackendTarget] = []
    for target in targets:
        if not target.available:
            skipped.append(target)
            continue
        for level in levels:
            items.append(WorkloadItem(target.name, target, level, split_roles(level)))
    return items, skipped


def run_workload(
    *,
    settings: BenchmarkSettings,
    targets: Sequence[BackendTarget] | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Measure the mixed scenario on each available target, and return the report.

    Args:
        settings: The run settings, with `workload` set.
        targets: The targets to measure. The default is `default_targets()`.
        progress: The reporter. The function tells it about each pass and each
            call.

    Returns:
        The report as plain data, with the same outer structure as the report
        of a case run. Each section has a `workloads` list, and its `cases`
        list is empty.
    """

    run_targets = tuple(targets if targets is not None else default_targets())
    items, skipped = plan_workload(run_targets, settings.workload_levels)
    started_at = datetime.now(UTC)
    started = perf_counter()
    total_passes = len(items) * len(WORKLOAD_PASSES)
    measured: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(items):
        entry = measure_workload_item(
            item,
            settings=settings,
            progress=progress,
            first_position=index * len(WORKLOAD_PASSES) + 1,
            total=total_passes,
        )
        measured.setdefault(item.section, []).append(entry)
    if progress is not None:
        progress.close()
    return {
        "schema": REPORT_SCHEMA,
        "started_at": started_at.isoformat(),
        "duration_seconds": perf_counter() - started,
        "environment": environment.describe(),
        "settings": settings.as_dict(),
        "layers": [MEMORY],
        "workers": list(settings.workload_levels),
        "sections": _build_workload_sections(run_targets, measured, skipped),
        "errors": _workload_total(measured, "errors"),
        "conflicts": _workload_total(measured, "conflicts"),
    }


def measure_workload_item(
    item: WorkloadItem,
    *,
    settings: BenchmarkSettings,
    progress: Progress | None = None,
    first_position: int = 1,
    total: int = len(WORKLOAD_PASSES),
) -> dict[str, Any]:
    """Measure the 3 passes of one mixed variant, in sequence.

    The order is fixed. The reads-alone pass uses the call counts from the
    mixed pass. So the function cannot plan it before the mixed pass runs.

    Args:
        item: The variant to measure.
        settings: The run settings.
        progress: The reporter. The function tells it about each pass and each
            call.
        first_position: The one-based position of the first pass of this
            variant in all of the run, for the progress line.
        total: The passes in all of the run.

    Returns:
        The report entry of the variant.
    """

    passes: dict[str, dict[str, Any]] = {}
    read_caps: dict[int, int] | None = None
    for offset, pass_name in enumerate(WORKLOAD_PASSES):
        if pass_name == PASS_MIXED:
            streams: tuple[str, ...] = STREAMS
            caps = None
        elif pass_name == PASS_READS_ALONE:
            streams = (STREAM_READ,)
            caps = read_caps
        else:
            streams = (STREAM_WRITE,)
            caps = None
        if progress is not None:
            progress.start_variant(first_position + offset, total, item.label(pass_name))
        passes[pass_name] = _measure_workload_pass(
            item,
            settings=settings,
            progress=progress,
            streams=streams,
            caps=caps,
        )
        if pass_name == PASS_MIXED:
            read_caps = {
                index: calls
                for index, calls in passes[pass_name]["worker_calls"].items()
                if item.roles.stream_of(index) == STREAM_READ
            }
    return _workload_entry(item, passes)


def build_workload_context(
    item: WorkloadItem,
    settings: BenchmarkSettings,
) -> tuple[CaseContext, Callable[[], None]]:
    """Open a new store, write the corpus through `Memory`, and wrap the store.

    The corpus always goes in through the public API, because both streams of
    this scenario also call the public API.

    Args:
        item: The variant that the runner measures next.
        settings: The run settings.

    Returns:
        The case context, and the function that disposes of the store.
    """

    backend, dispose = item.target.opener()
    memory = Memory(backend=backend, principal=PRINCIPAL_ID)
    corpus = dataset.build(settings)
    dataset.populate_memory(memory, corpus)
    context = CaseContext(
        settings=settings,
        corpus=corpus,
        backend=backend,
        memory=memory,
        principal_id=PRINCIPAL_ID,
        worker_count=item.workers,
    )
    return context, dispose


def _measure_workload_pass(
    item: WorkloadItem,
    *,
    settings: BenchmarkSettings,
    progress: Progress | None,
    streams: Sequence[str],
    caps: Mapping[int, int] | None,
) -> dict[str, Any]:
    """Prepare one pass, measure it, and dispose of its store.

    Args:
        item: The variant under measurement.
        settings: The run settings.
        progress: The reporter. The function tells it about each call.
        streams: The streams that this pass runs. The function creates only
            the workers in those streams. So in a baseline pass, the other
            stream does not run.
        caps: The measured calls for each worker. They apply to the workers
            that otherwise run until the stop signal.

    Returns:
        The pass result: one summary for each stream, the shared wall window,
        the setup time, and the number of measured calls that gave a result,
        for each worker.
    """

    setup_started = perf_counter()
    context, dispose = build_workload_context(item, settings)
    indexes = tuple(index for index in range(item.workers) if item.roles.stream_of(index) in streams)
    try:
        workers = {
            index: prepare_worker(replace(context, worker_index=index), item.roles)
            for index in indexes
        }
        setup_seconds = perf_counter() - setup_started
        result = _run_workload_pass(
            workers,
            caps=caps,
            settings=settings,
            progress=progress,
            setup_seconds=setup_seconds,
            scale={
                "corpus_blocks": context.corpus.block_count,
                "corpus_depth": context.corpus.depth,
            },
        )
    finally:
        dispose()
    return result


def _run_workload_pass(
    workers: Mapping[int, WorkloadWorker],
    *,
    caps: Mapping[int, int] | None,
    settings: BenchmarkSettings,
    progress: Progress | None,
    setup_seconds: float,
    scale: Mapping[str, Any],
) -> dict[str, Any]:
    """Time one pass, with all workers that call at the same time.

    The window:

    - **Warmup occurs before a barrier,** as for each concurrent variant. So
      the first call of a worker cannot occur in the measurement of a different
      worker.
    - **A worker with a call count stops when it gets to that count.** The
      count of a writer is the size of its range. The count of a baseline
      reader is the number of calls that it made in the mixed pass.
    - **A worker without a call count runs until the writers stop.** When the
      last writer gets to a barrier, the action of that barrier sets the stop
      event, and these workers stop. So the measured loop of a worker takes no
      lock.
    - **The wall time ends at the last return.** Each stream divides its own
      returned calls by that same window. So the stream rates add up to the
      total.

    Args:
        workers: The prepared workers of this pass. The keys are the worker
            indexes.
        caps: The measured calls for each worker, if a cap applies.
        settings: The run settings. The function reads the warmup count.
        progress: The reporter. This thread polls it while the workers run.
        setup_seconds: The wall time that the preparation of this pass
            already used.
        scale: Notes that describe the data of the calls.

    Returns:
        The pass result.

    Raises:
        RuntimeError: If a worker fails outside a measured call. Then the pass
            has no correct number to report.
    """

    gc.collect()
    order = tuple(sorted(workers))
    slots = {index: slot for slot, index in enumerate(order)}
    warmup = settings.warmup
    stop = Event()
    finished = [0] * len(order)
    outcomes = {index: _Outcome(workers[index].prepared.conflict_errors) for index in order}
    window: list[float] = []
    warmed = Barrier(len(order))
    measuring = Barrier(len(order), action=lambda: window.append(perf_counter()))
    writers = tuple(index for index in order if workers[index].stream == STREAM_WRITE)
    # The action occurs when the last writer gets to the barrier. This ends the
    # window for each reader that has no call count of its own.
    sweeping = Barrier(len(writers), action=stop.set) if writers else None

    def cap_of(index: int) -> int | None:
        """Return the measured calls for one worker, if it has a limit."""

        if caps is not None and index in caps:
            return caps[index]
        return workers[index].measured_calls

    def work(index: int) -> float:
        """Run the calls of one worker, and return the time of its last call."""

        worker = workers[index]
        outcome = outcomes[index]
        call = worker.prepared.call
        cap = cap_of(index)
        slot = slots[index]
        try:
            for step in range(warmup):
                # The worker discards the warmup results, also the failures.
                with suppress(Exception):
                    call(step)
                finished[slot] = step + 1
            warmed.wait()
            measuring.wait()
            step = warmup
            while not _workload_done(step, warmup, cap, stop):
                started = perf_counter_ns()
                try:
                    result = call(step)
                except Exception as error:  # noqa: BLE001 - a failing call is a result
                    outcome.record_failure(error)
                    # A call that raised an exception returns almost
                    # immediately. A worker without its own call count runs
                    # until the writers stop. So, if all calls of a stream
                    # fail, the worker loops here very fast and holds the
                    # interpreter lock. Then the workers that do real work get
                    # no time, and the window becomes much longer than the
                    # expected few milliseconds. The release of the lock adds
                    # no measured cost, because the runner never times a
                    # failed call.
                    sleep(0)
                else:
                    outcome.record_success(perf_counter_ns() - started, result)
                step += 1
                # Each worker writes the count into its own slot. So the progress
                # report never takes a lock that the other workers could wait
                # for.
                finished[slot] = step
            if sweeping is not None and worker.stream == STREAM_WRITE:
                sweeping.wait()
            return perf_counter()
        except BaseException:
            # If a worker stops before a barrier, the other workers wait and
            # never continue. So the worker breaks all barriers and releases
            # the readers.
            warmed.abort()
            measuring.abort()
            if sweeping is not None:
                sweeping.abort()
            stop.set()
            raise

    bounded = tuple(index for index in order if cap_of(index) is not None)
    loop_started = perf_counter()
    with ThreadPoolExecutor(max_workers=len(order)) as executor:
        futures = [executor.submit(work, index) for index in order]
        _follow_progress(
            progress,
            futures,
            finished,
            sum(warmup + (cap_of(index) or 0) for index in bounded),
            [slots[index] for index in bounded],
        )
        try:
            last_return = max(future.result() for future in futures)
        except Exception as error:
            raise RuntimeError(f"A benchmark worker failed outside a measured call: {error}") from error
    wall_seconds = last_return - window[0] if window else 0.0
    summaries = _workload_stream_summaries(
        workers,
        order,
        outcomes,
        wall_seconds=wall_seconds,
        setup_seconds=setup_seconds,
        scale=scale,
    )
    if progress is not None:
        first = next(iter(summaries.values()), {})
        progress.finish_variant(
            sum(result["samples"] for result in summaries.values()),
            perf_counter() - loop_started,
            first.get("p50_ms", 0.0),
        )
    return {
        "streams": summaries,
        "wall_seconds": wall_seconds,
        "setup_seconds": setup_seconds,
        # The calls that returned, not the calls that started.
        # - `throughput_per_second` uses the same rule.
        # - This rule makes the counts safe for a baseline pass. A call that
        #   raised an exception did no work. A repeat of it would measure the
        #   failure path, not the operation.
        # - If all calls of a reader fail, the reader loops as fast as the
        #   interpreter can. A count of the attempts would tell the baseline to
        #   repeat millions of failures, and not to fail fast.
        "worker_calls": {index: len(outcomes[index].durations) for index in order},
    }


def _workload_done(step: int, warmup: int, cap: int | None, stop: Event) -> bool:
    """Tell if one worker stops before its next call.

    Args:
        step: The calls that this worker already started, with the warmup
            calls.
        warmup: The calls that the worker discarded before the measurement.
        cap: The measured calls for this worker, or `None` if it runs until the
            writers stop.
        stop: The event that the last writer sets.

    Returns:
        `True` if the worker stops.
    """

    if cap is not None:
        return step >= warmup + cap
    return stop.is_set()


def _workload_stream_summaries(
    workers: Mapping[int, WorkloadWorker],
    order: Sequence[int],
    outcomes: Mapping[int, _Outcome],
    *,
    wall_seconds: float,
    setup_seconds: float,
    scale: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Merge the workers of one pass into one summary for each stream.

    Each stream divides its returned calls by the same `wall_seconds`. So the
    rates of the streams add up to the rate of all of the pass.

    Args:
        workers: The prepared workers of the pass.
        order: The worker indexes, in report order.
        outcomes: The results of the calls of each worker.
        wall_seconds: The wall time of the measured window.
        setup_seconds: The wall time to prepare the pass.
        scale: Notes that describe the data of the calls.

    Returns:
        One summary for each stream that ran, in report order.
    """

    summaries: dict[str, dict[str, Any]] = {}
    for stream in STREAMS:
        members = [index for index in order if workers[index].stream == stream]
        if not members:
            continue
        summaries[stream] = _summarize_outcomes(
            [outcomes[index] for index in members],
            prepared=workers[members[0]].prepared,
            workers=len(members),
            wall_seconds=wall_seconds,
            setup_seconds=setup_seconds,
            scale=dict(scale),
        ).as_dict()
    return summaries


def _workload_entry(item: WorkloadItem, passes: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Assemble the report entry of one mixed variant from its 3 passes.

    Args:
        item: The measured variant.
        passes: The result of each pass. The keys are the pass names.

    Returns:
        The entry. For each stream, it has the mixed result, the baseline, and
        the gap between the 2. It also has the 3 measured windows.
    """

    mixed = passes[PASS_MIXED]["streams"]
    baselines = {
        STREAM_READ: passes[PASS_READS_ALONE]["streams"].get(STREAM_READ),
        STREAM_WRITE: passes[PASS_WRITES_ALONE]["streams"].get(STREAM_WRITE),
    }
    total_calls = sum(result["samples"] for result in mixed.values())
    streams: list[dict[str, Any]] = []
    for stream in STREAMS:
        result = mixed.get(stream)
        if result is None:
            continue
        baseline = baselines[stream]
        entry: dict[str, Any] = {
            "stream": stream,
            "workers": result["workers"],
            "share_percent": result["samples"] / total_calls * 100.0 if total_calls else None,
            "result": result,
            "baseline": baseline,
            "p50_delta_ms": None,
            "p50_delta_percent": None,
        }
        if baseline and baseline["samples"] and result["samples"]:
            entry["p50_delta_ms"] = result["p50_ms"] - baseline["p50_ms"]
            if baseline["p50_ms"] > 0:
                entry["p50_delta_percent"] = entry["p50_delta_ms"] / baseline["p50_ms"] * 100.0
        streams.append(entry)
    windows = {name: passes[name]["wall_seconds"] for name in WORKLOAD_PASSES}
    sweep_alone = windows[PASS_WRITES_ALONE]
    return {
        "workers": item.workers,
        "readers": item.roles.readers,
        "writers": item.roles.writers,
        "setup_seconds": passes[PASS_MIXED]["setup_seconds"],
        "windows": windows,
        "sweep_ratio": windows[PASS_MIXED] / sweep_alone if sweep_alone > 0 else None,
        "streams": streams,
    }


def _build_workload_sections(
    targets: Sequence[BackendTarget],
    measured: Mapping[str, list[dict[str, Any]]],
    skipped: Sequence[BackendTarget],
) -> list[dict[str, Any]]:
    """Assemble the sections of the mixed report, in target order.

    Each section keeps an empty `cases` list. So a reader and the table
    renderer see the same section structure for each type of run.

    Args:
        targets: The targets that the run knew.
        measured: The variant entries for each target.
        skipped: The targets that could not run.

    Returns:
        One section for each target that ran or that the run skipped.
    """

    skip_reasons = {target.name: target.skip_reason for target in skipped}
    sections: list[dict[str, Any]] = []
    for target in targets:
        if target.name in skip_reasons:
            sections.append(
                {
                    "name": target.name,
                    "storage": target.storage,
                    "skipped": skip_reasons[target.name],
                    "cases": [],
                    "workloads": [],
                }
            )
            continue
        if target.name not in measured:
            continue
        sections.append(
            {
                "name": target.name,
                "storage": target.storage,
                "skipped": None,
                "cases": [],
                "workloads": list(measured[target.name]),
            }
        )
    return sections


def _workload_total(measured: Mapping[str, list[dict[str, Any]]], field: str) -> int:
    """Add one counter over all streams of all mixed variants.

    The function counts the mixed result and its baseline. A failure in a
    baseline pass is as real as a failure in the mixed pass.

    Args:
        measured: The variant entries for each target.
        field: The summary field to add, for example `"errors"`.

    Returns:
        The total.
    """

    total = 0
    for entries in measured.values():
        for entry in entries:
            for stream in entry["streams"]:
                for key in ("result", "baseline"):
                    result = stream[key]
                    if result:
                        total += result[field]
    return total


def _build_sections(
    targets: Sequence[BackendTarget],
    cases: Sequence[BenchmarkCase],
    layers: Sequence[str],
    results: Results,
    skipped: Sequence[BackendTarget],
) -> list[dict[str, Any]]:
    """Assemble the report sections in target order, with core last."""

    skip_reasons = {target.name: target.skip_reason for target in skipped}
    sections: list[dict[str, Any]] = []
    for target in targets:
        if target.name in skip_reasons:
            sections.append(
                {
                    "name": target.name,
                    "storage": target.storage,
                    "skipped": skip_reasons[target.name],
                    "cases": [],
                }
            )
            continue
        if target.name not in results:
            continue
        sections.append(
            {
                "name": target.name,
                "storage": target.storage,
                "skipped": None,
                "cases": _section_cases(cases, results[target.name]),
            }
        )
    if CORE_SECTION in results:
        sections.append(
            {
                "name": CORE_SECTION,
                "storage": CORE_STORAGE,
                "skipped": None,
                "cases": _section_cases(cases, results[CORE_SECTION]),
            }
        )
    del layers
    return sections


def _section_cases(
    cases: Sequence[BenchmarkCase],
    measured: dict[str, dict[str, dict[int, dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Assemble the cases of one section, in registry order.

    Each case has its single-caller variants, the gap between the 2 store
    layers, and one concurrency entry for each worker level above one.

    Args:
        cases: The cases, in registry order.
        measured: The summaries for this section. The keys are the case, the
            layer, and the worker count.

    Returns:
        One entry for each measured case.
    """

    section_cases: list[dict[str, Any]] = []
    for case in cases:
        levels = measured.get(case.name)
        if not levels:
            continue
        variants = {
            layer: by_worker[SINGLE_CALLER]
            for layer, by_worker in levels.items()
            if SINGLE_CALLER in by_worker
        }
        entry: dict[str, Any] = {
            "name": case.name,
            "group": case.group,
            "description": case.description,
            "variants": variants,
            "overhead_ms": None,
            "overhead_percent": None,
            "concurrency": _concurrency_entries(levels),
        }
        backend_result = variants.get(BACKEND)
        memory_result = variants.get(MEMORY)
        if backend_result and memory_result and backend_result["samples"] and memory_result["samples"]:
            entry["overhead_ms"] = memory_result["p50_ms"] - backend_result["p50_ms"]
            if backend_result["p50_ms"] > 0:
                entry["overhead_percent"] = entry["overhead_ms"] / backend_result["p50_ms"] * 100.0
        section_cases.append(entry)
    return section_cases


def _concurrency_entries(
    levels: dict[str, dict[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Describe the worker levels of one case above a single caller.

    `speedup` divides the wall-clock throughput of the level by the throughput
    of the single caller. It is `None` if the single-caller rate is not
    available or is 0, because then no correct divisor exists.

    Args:
        levels: The summaries for one case. The keys are the layer and the
            worker count.

    Returns:
        One entry for each layer and worker level above one, in report order.
    """

    entries: list[dict[str, Any]] = []
    for layer in (BACKEND, MEMORY, CORE):
        by_worker = levels.get(layer)
        if not by_worker:
            continue
        baseline = by_worker.get(SINGLE_CALLER, {}).get("throughput_per_second", 0.0)
        for workers in sorted(worker for worker in by_worker if worker > SINGLE_CALLER):
            result = by_worker[workers]
            entries.append(
                {
                    "layer": layer,
                    "workers": workers,
                    "speedup": result["throughput_per_second"] / baseline if baseline > 0 else None,
                    "result": result,
                }
            )
    return entries


def milliseconds(duration_ns: int) -> float:
    """Change a nanosecond duration to milliseconds.

    Args:
        duration_ns: The duration in nanoseconds.

    Returns:
        The same duration in milliseconds.
    """

    return duration_ns / NANOSECONDS_PER_MILLISECOND
