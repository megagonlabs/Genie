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
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter, perf_counter_ns
from typing import Any

from benchmarks import dataset, environment
from benchmarks.cases import BenchmarkCase, CaseContext, PreparedCase, default_cases
from benchmarks.metrics import NANOSECONDS_PER_MILLISECOND, Summary, summarize
from benchmarks.progress import Progress
from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, BenchmarkSettings
from benchmarks.targets import BackendTarget, default_targets
from genie import InMemoryBackend, Memory

REPORT_SCHEMA = 1
"""Version of the report shape. Raised when the JSON layout changes."""

CORE_SECTION = "core"
"""Section name for cases that measure the core library with no store."""

CORE_STORAGE = "core library only, no store behind it"
"""One-line description of the core section."""

PRINCIPAL_ID = "benchmark"
"""Principal that owns every block, so no case measures a denial path."""


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One variant to measure.

    Attributes:
        section: Report section this variant belongs to.
        target: Store to open, or `None` for a core-only variant.
        case: Case to measure.
        layer: Layer of the case to measure.
    """

    section: str
    target: BackendTarget | None
    case: BenchmarkCase
    layer: str

    @property
    def label(self) -> str:
        """Progress label naming the section, layer, and case."""

        return f"{self.section} {self.layer}: {self.case.name}"


def plan_run(
    targets: Sequence[BackendTarget],
    cases: Sequence[BenchmarkCase],
    layers: Sequence[str],
) -> tuple[list[WorkItem], list[BackendTarget]]:
    """Work out every variant a run will measure.

    Store-backed layers run once per available target. The `core` layer runs
    once in its own section, because a core helper has no store behind it and
    repeating it under every target would only duplicate the same number.

    Args:
        targets: Targets to measure.
        cases: Cases to measure.
        layers: Layers to measure.

    Returns:
        The variants to measure and the targets that were skipped.
    """

    store_layers = tuple(layer for layer in layers if layer in (BACKEND, MEMORY))
    items: list[WorkItem] = []
    skipped: list[BackendTarget] = []
    for target in targets:
        if not target.available:
            skipped.append(target)
            continue
        for case in cases:
            for layer in store_layers:
                if case.prepare_for(layer) is not None:
                    items.append(WorkItem(target.name, target, case, layer))
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
    items, skipped = plan_run(run_targets, run_cases, tuple(layers))
    started_at = datetime.now(UTC)
    started = perf_counter()
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for position, item in enumerate(items, start=1):
        if progress is not None:
            progress.start_variant(position, len(items), item.label)
        summary = measure_variant(item, settings=run_settings, progress=progress)
        results.setdefault(item.section, {}).setdefault(item.case.name, {})[item.layer] = (
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
        "sections": _build_sections(run_targets, run_cases, tuple(layers), results, skipped),
        "errors": sum(
            layer_result["errors"]
            for section in results.values()
            for case_result in section.values()
            for layer_result in case_result.values()
        ),
    }


def measure_variant(
    item: WorkItem,
    *,
    settings: BenchmarkSettings,
    progress: Progress | None = None,
) -> Summary:
    """Set up one variant, measure it, and dispose of its store.

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
        prepared = prepare(context)
        setup_seconds = perf_counter() - setup_started
        summary = measure(
            prepared,
            settings=settings,
            progress=progress,
            setup_seconds=setup_seconds,
            scale=_scale_notes(item, context),
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
    durations: list[int] = []
    errors = 0
    first_error: str | None = None
    last_result: Any = None
    total = settings.total_calls
    loop_started = perf_counter()
    for index in range(total):
        measured = index >= settings.warmup
        started = perf_counter_ns()
        try:
            result = prepared.call(index)
        except Exception as error:  # noqa: BLE001 - a failing call is a result, not a crash
            if measured:
                errors += 1
                if first_error is None:
                    first_error = f"{type(error).__name__}: {error}"
        else:
            elapsed = perf_counter_ns() - started
            if measured:
                durations.append(elapsed)
                last_result = result
        # Progress is reported only after the duration has been read, so writing
        # a line can never inflate a measurement.
        if progress is not None:
            progress.call_done(index + 1, total)
    loop_seconds = perf_counter() - loop_started
    notes: dict[str, Any] = dict(scale or {})
    if prepared.describe is not None and last_result is not None:
        notes.update(prepared.describe(last_result))
    summary = summarize(
        durations,
        errors=errors,
        first_error=first_error,
        setup_seconds=setup_seconds,
        notes=notes,
    )
    if progress is not None:
        progress.finish_variant(summary.samples, loop_seconds, summary.p50_ms)
    return summary


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
    results: dict[str, dict[str, dict[str, Any]]],
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
    measured: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble one section's cases in registry order, with the layer gap."""

    section_cases: list[dict[str, Any]] = []
    for case in cases:
        variants = measured.get(case.name)
        if not variants:
            continue
        entry: dict[str, Any] = {
            "name": case.name,
            "group": case.group,
            "description": case.description,
            "variants": variants,
            "overhead_ms": None,
            "overhead_percent": None,
        }
        backend_result = variants.get(BACKEND)
        memory_result = variants.get(MEMORY)
        if backend_result and memory_result and backend_result["samples"] and memory_result["samples"]:
            entry["overhead_ms"] = memory_result["p50_ms"] - backend_result["p50_ms"]
            if backend_result["p50_ms"] > 0:
                entry["overhead_percent"] = entry["overhead_ms"] / backend_result["p50_ms"] * 100.0
        section_cases.append(entry)
    return section_cases


def milliseconds(duration_ns: int) -> float:
    """Convert a nanosecond duration to milliseconds.

    Args:
        duration_ns: Duration in nanoseconds.

    Returns:
        The same duration in milliseconds.
    """

    return duration_ns / NANOSECONDS_PER_MILLISECOND
