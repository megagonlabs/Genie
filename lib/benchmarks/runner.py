"""Runs the benchmark cases and returns a report.

This module never touches a stream, which is what lets a test drive a whole run
without a terminal. Printing belongs to `report` and `run`.

What one measured variant costs:

1. **Setup.** A fresh backend is opened, the corpus is written, and the case
   prepares its own targets. The wall time is reported as `setup_seconds` and
   is excluded from every latency number.
2. **Warmup.** `settings.warmup` calls are made and thrown away, so the
   first-call cost of a connection, a cache, or a query plan stays out of the
   percentiles.
3. **Measurement.** `settings.iterations` calls are timed one at a time with
   `time.perf_counter_ns`. A call that raises is counted, not timed.
4. **Disposal.** The backend is closed and its files are removed, so nothing
   one variant wrote can move the next variant's numbers.

At more than one worker the same four steps run, with three differences:

- **Setup still runs in the calling thread,** once per worker and one worker at
  a time, so preparation can never race and is never timed.
- **A barrier starts the measured window for every worker at the same instant.**
  Warmup happens before it, so a slow first call cannot land inside another
  worker's measurement.
- **Progress is polled, not pushed.** Each worker writes its own finished-call
  count into its own slot and the calling thread sums those slots. No lock
  enters a worker's hot path, so reporting cannot distort throughput.
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Barrier
from time import perf_counter, perf_counter_ns
from typing import Any

from benchmarks import dataset, environment
from benchmarks.cases import BenchmarkCase, CaseContext, PreparedCase, default_cases
from benchmarks.metrics import NANOSECONDS_PER_MILLISECOND, Summary, summarize
from benchmarks.progress import Progress
from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, SINGLE_CALLER, BenchmarkSettings
from benchmarks.targets import BackendTarget, default_targets
from genie import InMemoryBackend, Memory

REPORT_SCHEMA = 2
"""Version of the report shape. Raised when the JSON layout changes."""

CORE_SECTION = "core"
"""Section name for cases that measure the core library with no store."""

CORE_STORAGE = "core library only, no store behind it"
"""One-line description of the core section."""

PRINCIPAL_ID = "benchmark"
"""Principal that owns every block, so no case measures a denial path."""

Results = dict[str, dict[str, dict[str, dict[int, dict[str, Any]]]]]
"""Measured summaries, keyed by section, case name, layer, and worker count."""


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One variant to measure.

    Attributes:
        section: Report section this variant belongs to.
        target: Store to open, or `None` for a core-only variant.
        case: Case to measure.
        layer: Layer of the case to measure.
        workers: Callers that run the case at the same time, sharing one store
            and one `Memory`.
    """

    section: str
    target: BackendTarget | None
    case: BenchmarkCase
    layer: str
    workers: int = SINGLE_CALLER

    @property
    def label(self) -> str:
        """Progress label naming the section, layer, case, and worker count."""

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
    """Work out every variant a run will measure.

    Store-backed layers run once per available target and once per worker
    level. The worker levels of one case and layer are planned next to each
    other, so the runs being compared happen close together and a drifting
    machine cannot move `speedup` on its own.

    The `core` layer runs once in its own section at a single caller. A core
    helper has no store behind it, so repeating it under every target would
    only duplicate the same number, and contention cannot mean anything there.

    Args:
        targets: Targets to measure.
        cases: Cases to measure.
        layers: Layers to measure.
        workers: Worker levels to measure, smallest first.

    Returns:
        The variants to measure and the targets that were skipped.
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
    """Measure every selected variant and return the report.

    A case marked `concurrent_only` is measured only when the settings ask for
    more than one worker, so a default run keeps the length and the shape it
    always had.

    Args:
        settings: Run settings. Defaults to `BenchmarkSettings()`.
        targets: Targets to measure. Defaults to `default_targets()`.
        cases: Cases to measure. Defaults to `default_cases()`.
        layers: Layers to measure. Defaults to every layer.
        progress: Reporter told about each call after its duration was read.

    Returns:
        The report as plain data, ready for the table renderer or the JSON
        writer.
    """

    run_settings = settings or BenchmarkSettings()
    run_targets = tuple(targets if targets is not None else default_targets())
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
    """Add up one counter over every measured variant.

    Args:
        results: Measured summaries.
        field: Summary field to add up, such as `"errors"`.

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
    """Set up one variant, measure it, and dispose of its store.

    Every worker is prepared in this thread, one at a time, so setup can never
    race and is never timed. Worker `0` performs the setup the workers share,
    such as creating the block a contention case fights over.

    Args:
        item: Variant to measure.
        settings: Run settings.
        progress: Reporter told about each call after its duration was read.

    Returns:
        The variant's summary.
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
    """Open a fresh store, write the corpus, and wrap it in a context.

    Args:
        item: Variant about to be measured.
        settings: Run settings.

    Returns:
        The case context and the function that disposes of the store.
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
        # A core helper has no store behind it, so only the smallest corpus the
        # case context needs is described, and nothing is written.
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

    Garbage collection runs once before the loop and stays enabled, because a
    caller in production pays that cost too.

    Args:
        prepared: Case ready to be called.
        settings: Run settings.
        progress: Reporter told about each call after its duration was read.
        setup_seconds: Wall time already spent preparing this variant.
        scale: Notes describing the data the calls ran against.

    Returns:
        The variant's summary.
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
        # Progress is reported only after the duration has been read, so writing
        # a line can never inflate a measurement.
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
    """Time one prepared case with several workers calling it at once.

    Every worker shares the store and the `Memory` above it, which is the shape
    an application has. Each worker runs its own prepared case, so an ordinary
    case writes ids nothing else touches while a `contention` case aims every
    worker at one target.

    How the measured window is defined:

    - **Warmup happens before a barrier.** Once every worker has warmed up, the
      barrier releases them together and stamps the window's start, so no
      worker's first call lands inside another worker's measurement.
    - **Wall time runs to the last worker's last return,** and
      `throughput_per_second` is the calls that returned divided by it. That is
      the rate every worker together achieved, next to the per-caller rate
      `ops_per_second` still reports.
    - **Latency percentiles come from every worker's durations merged,** so one
      slow worker widens the tail rather than hiding in an average.

    Args:
        prepared: One prepared case per worker.
        settings: Run settings. `iterations` and `warmup` count per worker.
        progress: Reporter polled by this thread while the workers run.
        setup_seconds: Wall time already spent preparing this variant.
        scale: Notes describing the data the calls ran against.

    Returns:
        The variant's summary.

    Raises:
        RuntimeError: If a worker fails outside a measured call, which leaves
            the run with no honest number to report.
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
        """Run one worker's calls and return when it finished the last one."""

        call = prepared[worker].call
        outcome = outcomes[worker]
        try:
            for index in range(settings.warmup):
                # Warmup results are thrown away, failures included, so the
                # first call of a connection or a query plan cannot enter the
                # percentiles either way.
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
                # The count is this worker's own slot, so reporting never takes
                # a lock the other workers could queue behind.
                finished_calls[worker] = index + 1
            return perf_counter()
        except BaseException:
            # A worker that dies before a barrier would leave the others waiting
            # forever, so both gates are broken on the way out.
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
    """What one caller's measured calls did.

    A conflict is separated from an error here, because a `contention` case
    expects to lose races and a lost race must never fail a run.

    Attributes:
        conflict_errors: Exception types counted as conflicts.
        durations: Nanosecond durations of the calls that returned.
        errors: Calls that raised something unexpected.
        conflicts: Calls that lost a documented race.
        first_error: Type and message of the first unexpected failure.
        last_result: Last result that returned, used for the notes.
    """

    __slots__ = ("conflict_errors", "conflicts", "durations", "errors", "first_error", "last_result")

    def __init__(self, conflict_errors: tuple[type[BaseException], ...]) -> None:
        """Create an empty outcome.

        Args:
            conflict_errors: Exception types counted as conflicts.
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
            duration_ns: How long it took.
            result: What it returned.
        """

        self.durations.append(duration_ns)
        self.last_result = result

    def record_failure(self, error: Exception) -> None:
        """Count a call that raised, as a conflict or as an error.

        The duration is dropped either way, so a call that did not finish its
        work cannot enter the latency percentiles.

        Args:
            error: What the call raised.
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
    """Merge every worker's outcome into one summary.

    Args:
        outcomes: One outcome per worker.
        prepared: Prepared case, read for its `describe` function.
        workers: Callers that ran at the same time.
        wall_seconds: Wall time of the measured window.
        setup_seconds: Wall time spent preparing the variant.
        scale: Notes describing the data the calls ran against.

    Returns:
        The variant's summary.
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
) -> None:
    """Report what the workers have finished until every one of them is done.

    The workers never call the reporter themselves. This thread reads their
    counters instead, so no lock enters a worker's hot path and reporting
    cannot distort throughput.

    Args:
        progress: Reporter, or `None` to wait quietly.
        futures: One future per worker.
        finished_calls: One counter per worker, written by that worker only.
        total: Calls the whole variant will make, warmup included.
    """

    if progress is None:
        return
    pending = list(futures)
    while pending:
        _, not_done = wait(pending, timeout=progress.interval)
        progress.call_done(sum(finished_calls), total)
        pending = list(not_done)


def _scale_notes(item: WorkItem, context: CaseContext) -> dict[str, Any]:
    """Describe the data a variant ran against."""

    if not item.case.needs_corpus:
        return {}
    return {
        "corpus_blocks": context.corpus.block_count,
        "corpus_depth": context.corpus.depth,
    }


def _build_sections(
    targets: Sequence[BackendTarget],
    cases: Sequence[BenchmarkCase],
    layers: Sequence[str],
    results: Results,
    skipped: Sequence[BackendTarget],
) -> list[dict[str, Any]]:
    """Assemble the report sections in target order, core last."""

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
    """Assemble one section's cases in registry order.

    Each case carries its single-caller variants, the gap between the two store
    layers, and one concurrency entry per worker level above one.

    Args:
        cases: Cases in registry order.
        measured: Summaries for this section, keyed by case, layer, and worker
            count.

    Returns:
        One entry per measured case.
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
    """Describe one case's worker levels above a single caller.

    `speedup` divides the level's wall-clock throughput by the single caller's.
    It is `None` when the single-caller rate is missing or zero, because there
    is then nothing honest to divide by.

    Args:
        levels: Summaries for one case, keyed by layer and worker count.

    Returns:
        One entry per layer and worker level above one, in report order.
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
    """Convert a nanosecond duration to milliseconds.

    Args:
        duration_ns: Duration in nanoseconds.

    Returns:
        The same duration in milliseconds.
    """

    return duration_ns / NANOSECONDS_PER_MILLISECOND
