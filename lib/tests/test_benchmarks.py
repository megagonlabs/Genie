"""Tests for the benchmark harness under `lib/benchmarks/`.

- **Why these tests exist.** The harness is not a test suite, and no code in it
  asserts. These tests make sure that the harness continues to work.
- **Speed.** The tests use tiny settings, so the suite stays fast.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from benchmarks import report, run, runner
from benchmarks.cases import HOT_PARENT_ID, CaseContext, PreparedCase, default_cases, select
from benchmarks.metrics import percentile, summarize
from benchmarks.progress import PLAIN_INTERVAL_SECONDS, TERMINAL_INTERVAL_SECONDS, for_stream
from benchmarks.report import CONCURRENCY_HEADERS, LATENCY_HEADERS, WORKLOAD_HEADERS
from benchmarks.runner import CORE_SECTION, REPORT_SCHEMA, WorkItem, WorkloadItem
from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, BenchmarkSettings
from benchmarks.targets import BackendTarget, clear_store, default_targets, server_targets
from benchmarks.workload import (
    MINIMUM_WORKERS,
    READ_PER_WRITE,
    STREAM_READ,
    STREAM_WRITE,
    WARMUP_BLOCK_PREFIX,
    WorkloadWorker,
    prepare_worker,
    read_offset,
    split_roles,
    write_range,
)
from genie import InMemoryBackend, Memory, MemoryBlock, MemoryConflict, MemoryQuery, Scope

TINY = BenchmarkSettings(
    iterations=2,
    warmup=1,
    blocks=6,
    sections=2,
    value_size=4,
    links_per_block=2,
    search_limit=3,
)
"""Small `BenchmarkSettings` that run the whole harness inside a unit test."""

WORKERS = 3
"""The number of callers that the concurrency tests use. It is large enough to interleave calls and small enough to keep the tests fast."""

TINY_CONCURRENT = BenchmarkSettings(
    iterations=2,
    warmup=1,
    blocks=6,
    sections=2,
    value_size=4,
    links_per_block=2,
    search_limit=3,
    workers=(1, WORKERS),
)
"""The `TINY` settings, measured at 1 caller and at `WORKERS` callers."""


def _quiet() -> Any:
    """Return a progress reporter that writes nothing."""

    return for_stream(io.StringIO(), quiet=True)


class _FakeTerminal(io.StringIO):
    """Stream that reports itself as a terminal, so the progress reporter rewrites one line."""

    def isatty(self) -> bool:
        """Return `True` to report a terminal, whatever the real stream is."""

        return True


def test_every_case_runs_at_every_layer_it_declares() -> None:
    """A single-caller run measures each case at each of its layers and records no errors."""

    result = runner.run(settings=TINY, progress=_quiet())

    assert result["schema"] == REPORT_SCHEMA
    assert result["errors"] == 0
    measured = {
        (section["name"], case["name"], layer)
        for section in result["sections"]
        for case in section["cases"]
        for layer in case["variants"]
    }
    single_caller_cases = select(default_cases())
    available = [target for target in default_targets() if target.available]
    assert [target.name for target in available[:2]] == ["in_memory", "sqlite"]
    for target in available:
        for case in single_caller_cases:
            for layer in (BACKEND, MEMORY):
                if case.prepare_for(layer) is not None:
                    assert (target.name, case.name, layer) in measured
    for case in single_caller_cases:
        if case.prepare_for(CORE) is not None:
            assert (CORE_SECTION, case.name, CORE) in measured


def test_every_variant_reports_samples_and_the_scale_it_ran_against() -> None:
    """Each variant reports its measured calls, and each store case reports the corpus scale."""

    result = runner.run(settings=TINY, progress=_quiet())

    for section in result["sections"]:
        for case in section["cases"]:
            for layer, variant in case["variants"].items():
                assert variant["samples"] == TINY.iterations
                assert variant["setup_seconds"] >= 0.0
                if layer == CORE:
                    assert "corpus_blocks" not in variant["notes"]
                else:
                    assert variant["notes"]["corpus_blocks"] > 0
                    assert variant["notes"]["corpus_depth"] > 0


def test_the_two_layers_of_one_case_see_an_identical_corpus() -> None:
    """The difference between the two layers is a fair measure only if both layers wrote the same data."""

    case = next(item for item in default_cases() if item.name == "get")
    stored: dict[str, dict[str, Any]] = {}
    for layer in (BACKEND, MEMORY):
        item = WorkItem("in_memory", default_targets()[0], case, layer)
        context, dispose = runner.build_context(item, TINY)
        try:
            stored[layer] = {
                block_id: (block.type, block.value, block.parent_id, block.metadata, block.version)
                for block_id, block in context.backend._blocks.items()
            }
        finally:
            dispose()

    assert stored[BACKEND].keys() == stored[MEMORY].keys()
    assert stored[BACKEND] == stored[MEMORY]


def test_search_cases_report_what_one_call_returned() -> None:
    """The notes show the `returned` count next to the `limit` of the search, so a short page is visible."""

    cases = select(default_cases(), names=("search",))
    result = runner.run(settings=TINY, cases=cases, progress=_quiet())

    for section in result["sections"]:
        for case in section["cases"]:
            for variant in case["variants"].values():
                assert variant["notes"]["limit"] == TINY.search_limit
                assert variant["notes"]["returned"] <= TINY.search_limit


def test_a_paired_case_reports_the_layer_gap_and_a_single_layer_case_does_not() -> None:
    """Only a case measured at both layers reports the overhead that `Memory` adds."""

    cases = select(default_cases(), names=("get", "merge_value"))
    result = runner.run(settings=TINY, cases=cases, layers=(BACKEND, MEMORY), progress=_quiet())
    entries = {
        case["name"]: case for section in result["sections"] for case in section["cases"]
    }

    assert entries["get"]["overhead_ms"] is not None
    assert entries["get"]["overhead_percent"] is not None
    assert entries["merge_value"]["overhead_ms"] is None
    assert entries["merge_value"]["overhead_percent"] is None


def test_core_cases_run_once_in_their_own_section() -> None:
    """A core helper has no store, so a separate run for each target gives no new information."""

    cases = select(default_cases(), groups=("overhead",))
    available = tuple(target for target in default_targets() if target.available)
    items, skipped = runner.plan_run(available, cases, LAYERS)

    assert not skipped
    assert {item.section for item in items} == {CORE_SECTION}
    assert len(items) == len(cases)


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------


def _measured(result: dict[str, Any], section: str, case: str, layer: str, workers: int) -> dict[str, Any]:
    """Return one measured variant, whatever worker level it ran at.

    Args:
        result: Report returned by the runner.
        section: Section name, such as `"in_memory"`.
        case: Case name.
        layer: Layer name.
        workers: Worker level to look for.

    Returns:
        The summary of that variant.
    """

    entry = next(
        item
        for report_section in result["sections"]
        if report_section["name"] == section
        for item in report_section["cases"]
        if item["name"] == case
    )
    if workers == 1:
        return entry["variants"][layer]
    return next(
        level["result"]
        for level in entry["concurrency"]
        if level["layer"] == layer and level["workers"] == workers
    )


def test_every_worker_of_a_variant_is_measured() -> None:
    """The latency comes from the merged calls of every worker, not from one worker."""

    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND,),
        progress=_quiet(),
    )
    measured = _measured(result, "in_memory", "get", BACKEND, WORKERS)

    assert measured["workers"] == WORKERS
    assert measured["samples"] == TINY_CONCURRENT.iterations * WORKERS
    assert measured["errors"] == 0


def test_the_single_caller_level_is_measured_even_when_it_was_not_asked_for() -> None:
    """`speedup` needs a denominator, so the settings always include level 1."""

    settings = BenchmarkSettings(iterations=2, warmup=1, blocks=6, sections=2, value_size=4, links_per_block=2, search_limit=3, workers=(WORKERS,))
    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=settings,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND,),
        progress=_quiet(),
    )

    assert settings.workers == (1, WORKERS)
    assert _measured(result, "in_memory", "get", BACKEND, 1)["workers"] == 1
    assert _measured(result, "in_memory", "get", BACKEND, WORKERS)["workers"] == WORKERS


def test_workers_never_write_the_same_id() -> None:
    """Every write case gives each worker its own ids, so no ids collide.

    A collision appears as an error because a repeated id raises
    `MemoryConflict` and no write case expects that conflict.
    """

    cases = select(default_cases(), groups=("write",), concurrent=True)
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND, MEMORY),
        progress=_quiet(),
    )

    assert result["errors"] == 0
    assert result["conflicts"] == 0
    for section in result["sections"]:
        for case in section["cases"]:
            for level in case["concurrency"]:
                assert level["result"]["samples"] == TINY_CONCURRENT.iterations * WORKERS


def test_setup_every_worker_shares_runs_once() -> None:
    """The hub links are corpus state, so more workers do not add more links."""

    cases = select(default_cases(), names=("list_links",))
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND,),
        progress=_quiet(),
    )
    single = _measured(result, "in_memory", "list_links", BACKEND, 1)
    concurrent = _measured(result, "in_memory", "list_links", BACKEND, WORKERS)

    assert single["notes"]["returned"] == TINY_CONCURRENT.links_per_block
    assert concurrent["notes"]["returned"] == single["notes"]["returned"]


def test_a_contention_case_aims_every_worker_at_one_target() -> None:
    """Every worker writes under one shared parent, and every child gets an id that no other write uses."""

    case = next(item for item in default_cases() if item.name == "hot_parent_put")
    prepare = case.prepare_for(BACKEND)
    assert prepare is not None
    item = WorkItem("in_memory", default_targets()[0], case, BACKEND, WORKERS)
    context, dispose = runner.build_context(item, TINY_CONCURRENT)
    try:
        prepared = [
            prepare(replace(context, worker_index=worker)) for worker in range(WORKERS)
        ]
        for worker in range(WORKERS):
            for index in range(TINY_CONCURRENT.total_calls):
                prepared[worker].call(index)
        children = context.backend.children(HOT_PARENT_ID)
    finally:
        dispose()

    assert len(children) == TINY_CONCURRENT.total_calls * WORKERS
    assert len({child.id for child in children}) == len(children)


def test_contention_cases_stay_out_of_a_single_caller_run() -> None:
    """A shared target with one caller has no contention, so a single-caller run does not measure contention cases."""

    single = {case.name for case in select(default_cases())}
    concurrent = {case.name for case in select(default_cases(), concurrent=True)}
    result = runner.run(settings=TINY, progress=_quiet())
    measured = {
        case["name"] for section in result["sections"] for case in section["cases"]
    }

    assert "hot_block_set" in concurrent - single
    assert not measured & (concurrent - single)


def test_contention_cases_run_at_every_level_once_concurrency_is_asked_for() -> None:
    """Contention cases also run at level 1, because `speedup` divides by the level 1 result."""

    cases = select(default_cases(), groups=("contention",), concurrent=True)
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND, MEMORY),
        progress=_quiet(),
    )
    entry = next(
        case
        for section in result["sections"]
        for case in section["cases"]
        if case["name"] == "hot_block_set"
    )

    assert list(entry["variants"]) == [MEMORY]
    assert [(level["layer"], level["workers"]) for level in entry["concurrency"]] == [
        (MEMORY, WORKERS)
    ]


def test_a_hot_block_write_expects_to_lose_races() -> None:
    """A contention write declares `MemoryConflict` as the conflict that it causes on purpose."""

    for name in ("hot_block_set", "hot_block_merge"):
        case = next(item for item in default_cases() if item.name == name)
        prepare = case.prepare_for(MEMORY)
        assert prepare is not None
        item = WorkItem("in_memory", default_targets()[0], case, MEMORY, WORKERS)
        context, dispose = runner.build_context(item, TINY_CONCURRENT)
        try:
            prepared = prepare(context)
        finally:
            dispose()

        assert prepared.conflict_errors == (MemoryConflict,)


def test_a_conflict_is_counted_apart_from_an_error() -> None:
    """A contention case expects a lost race, so the runner counts it as a conflict, not as an error."""

    def losing(_: int) -> None:
        raise MemoryConflict("another worker wrote first")

    summary = runner.measure_concurrent(
        [PreparedCase(call=losing, conflict_errors=(MemoryConflict,))] * WORKERS,
        settings=TINY_CONCURRENT,
    )

    assert summary.conflicts == TINY_CONCURRENT.iterations * WORKERS
    assert summary.errors == 0
    assert summary.first_error is None
    assert summary.samples == 0


def test_an_undeclared_conflict_is_still_an_error() -> None:
    """The runner accepts only the conflict types that a case declares and counts every other exception as an error."""

    def losing(_: int) -> None:
        raise MemoryConflict("another worker wrote first")

    summary = runner.measure_concurrent(
        [PreparedCase(call=losing)] * WORKERS,
        settings=TINY_CONCURRENT,
    )

    assert summary.errors == TINY_CONCURRENT.iterations * WORKERS
    assert summary.conflicts == 0
    assert summary.first_error is not None


def test_conflicts_never_change_the_exit_status() -> None:
    """Times and lost races are results, so only an unexpected error changes the exit status."""

    conflicting = PreparedCase(call=_raise_conflict, conflict_errors=(MemoryConflict,))
    case = default_cases()[0]
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=(replace(case, backend=lambda context: conflicting, memory=None),),
        layers=(BACKEND,),
        progress=_quiet(),
    )

    assert result["errors"] == 0
    assert result["conflicts"] > 0


def _raise_conflict(_: int) -> None:
    """Raise `MemoryConflict`, as a hot-block write does when it loses a race."""

    raise MemoryConflict("another worker wrote first")


def test_throughput_is_the_wall_rate_and_speedup_divides_by_one_caller() -> None:
    """`throughput_per_second` is the wall rate, and `speedup` divides it by the single-caller rate, because they answer different questions."""

    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND,),
        progress=_quiet(),
    )
    entry = next(
        case
        for section in result["sections"]
        for case in section["cases"]
        if case["name"] == "get"
    )
    single = entry["variants"][BACKEND]
    level = next(item for item in entry["concurrency"] if item["workers"] == WORKERS)

    assert single["wall_seconds"] > 0.0
    assert level["result"]["wall_seconds"] > 0.0
    assert level["result"]["throughput_per_second"] == pytest.approx(
        level["result"]["samples"] / level["result"]["wall_seconds"]
    )
    assert level["speedup"] == pytest.approx(
        level["result"]["throughput_per_second"] / single["throughput_per_second"]
    )


def test_core_cases_are_measured_at_one_caller_only() -> None:
    """A core helper has no store, so more callers cannot cause contention."""

    cases = select(default_cases(), groups=("overhead",))
    available = tuple(target for target in default_targets() if target.available)
    items, _ = runner.plan_run(available, cases, LAYERS, (1, WORKERS))

    assert {item.workers for item in items} == {1}


def test_the_worker_count_reaches_the_progress_label() -> None:
    """A run can take minutes, so the progress label shows the worker level."""

    case = default_cases()[0]

    assert WorkItem("sqlite", None, case, MEMORY).label == f"sqlite memory: {case.name}"
    assert WorkItem("sqlite", None, case, MEMORY, 4).label == f"sqlite memory: {case.name} x4"


@pytest.mark.parametrize(
    ("workers", "expected"),
    [((1,), (1,)), ((4,), (1, 4)), ((8, 2, 2), (1, 2, 8))],
)
def test_worker_levels_are_sorted_deduplicated_and_start_at_one(
    workers: tuple[int, ...],
    expected: tuple[int, ...],
) -> None:
    """Two runs that ask for the same levels report the same rows."""

    settings = BenchmarkSettings(workers=workers)

    assert settings.workers == expected
    assert settings.measures_concurrency is (max(expected) > 1)


@pytest.mark.parametrize("workers", [(0,), (-1,), ("4",), 4, (True,)])
def test_settings_reject_worker_levels_that_cannot_describe_a_run(workers: Any) -> None:
    """A level below one, or one that is not a number, is a usage error."""

    with pytest.raises((TypeError, ValueError)):
        BenchmarkSettings(workers=workers)


def test_unavailable_targets_are_planned_as_skipped() -> None:
    """The plan reports a server with no connection variables as skipped and does not omit it."""

    cases = select(default_cases(), names=("get",))
    items, skipped = runner.plan_run(default_targets(), cases, LAYERS)

    assert [target.name for target in skipped] == [
        target.name for target in default_targets() if not target.available
    ]
    for target in skipped:
        assert target.skip_reason
        assert all(item.section != target.name for item in items)


def test_server_targets_name_exactly_what_is_missing() -> None:
    """A skip reason tells the user what to do, so it names the connection variable to set."""

    reasons = {target.name: target.skip_reason for target in server_targets()}

    assert set(reasons) == {"postgres", "mongodb", "neo4j", "redis"}
    for name, variable in (
        ("postgres", "GENIE_POSTGRES_DSN"),
        ("mongodb", "GENIE_MONGODB_URI"),
        ("neo4j", "GENIE_NEO4J_URI"),
        ("redis", "GENIE_REDIS_URL"),
    ):
        reason = reasons[name]
        if reason is not None and "install" not in reason:
            assert variable in reason


def test_a_skipped_target_reports_its_reason_and_runs_no_case() -> None:
    """The report shows a target that cannot run as skipped, never as a pass."""

    unavailable = BackendTarget(
        name="postgres",
        storage="PostgresBackend on a server",
        opener=lambda: (None, lambda: None),
        skip_reason="set GENIE_POSTGRES_DSN to measure it",
    )
    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=TINY,
        targets=(unavailable,),
        cases=cases,
        layers=(BACKEND, MEMORY),
        progress=_quiet(),
    )
    section = next(item for item in result["sections"] if item["name"] == "postgres")

    assert section["skipped"] == "set GENIE_POSTGRES_DSN to measure it"
    assert section["cases"] == []
    assert "skipped" in report.format_report(result)


def test_clear_store_empties_a_store_through_the_public_protocol() -> None:
    """The harness cannot discard a server between variants, so `clear_store` empties it.

    The reset uses only `search`, `delete_block`, and the cascade from the
    delete contract, so it works on any adapter and needs no adapter internals.
    """

    backend = InMemoryBackend()
    memory = Memory(backend=backend, principal="benchmark")
    root = memory.put(type="root", value="root")
    child = memory.put(type="child", value="child", parent=root.id)
    grandchild = memory.put(type="leaf", value="leaf", parent=child.id)
    other_root = memory.put(type="root", value="other")
    memory.add_link(grandchild.id, other_root.id, "bench.related")

    clear_store(backend)

    assert backend.search(MemoryQuery(scope=Scope.GLOBAL, limit=1000)) == []
    for block in (root, child, grandchild, other_root):
        assert backend.get_block(block.id) is None
    assert backend.list_links(grandchild.id, direction="both") == []


def test_clear_store_accepts_a_store_that_is_already_empty() -> None:
    """The reset on open does not depend on the data that the last run left in the store."""

    backend = InMemoryBackend()

    clear_store(backend)
    clear_store(backend)

    assert backend.search(MemoryQuery(scope=Scope.GLOBAL, limit=10)) == []


def test_a_failing_call_is_counted_and_not_timed() -> None:
    """The runner counts a failed call as an error and does not record its time as a very fast call."""

    def call(index: int) -> Any:
        raise RuntimeError(f"call {index} refused")

    summary = runner.measure(PreparedCase(call=call), settings=TINY)

    assert summary.samples == 0
    assert summary.errors == TINY.iterations
    assert summary.first_error == f"RuntimeError: call {TINY.warmup} refused"
    assert summary.p50_ms == 0.0
    assert summary.ops_per_second == 0.0


def test_only_measured_calls_are_counted() -> None:
    """The runner makes the warmup calls and discards their results, failures included."""

    seen: list[int] = []

    def call(index: int) -> int:
        seen.append(index)
        if index < TINY.warmup:
            raise RuntimeError("warmup failure is not reported")
        return index

    summary = runner.measure(PreparedCase(call=call), settings=TINY)

    assert seen == list(range(TINY.total_calls))
    assert summary.samples == TINY.iterations
    assert summary.errors == 0
    assert summary.first_error is None


def test_notes_describe_the_last_measured_result() -> None:
    """A case describes what its calls did, so the reader sees each number in context."""

    prepared = PreparedCase(
        call=lambda index: [index] * 3,
        describe=lambda result: {"returned": len(result)},
    )

    summary = runner.measure(prepared, settings=TINY, scale={"corpus_blocks": 8})

    assert summary.notes == {"corpus_blocks": 8, "returned": 3}


def test_percentiles_use_nearest_rank() -> None:
    """Every reported value is a real measurement, never an interpolation."""

    values = [float(number) for number in range(1, 101)]

    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 0.95) == 95.0
    assert percentile(values, 0.99) == 99.0
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0
    assert percentile([], 0.5) == 0.0
    assert percentile([7.0], 0.99) == 7.0
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        percentile(values, 1.5)


def test_summarize_reports_zeros_when_every_call_failed() -> None:
    """A variant with no timed calls still gives a report and does not crash."""

    summary = summarize([], errors=4, first_error="ValueError: no")

    assert summary.samples == 0
    assert summary.errors == 4
    assert summary.as_dict()["p99_ms"] == 0.0
    assert summary.as_dict()["notes"] == {}


def test_summarize_reports_one_caller_rate_from_its_own_call_time() -> None:
    """`ops_per_second` comes from the time the calls took, not wall time."""

    summary = summarize([1_000_000, 2_000_000, 3_000_000])

    assert summary.samples == 3
    assert summary.min_ms == 1.0
    assert summary.max_ms == 3.0
    assert summary.mean_ms == pytest.approx(2.0)
    assert summary.ops_per_second == pytest.approx(500.0)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"iterations": 0}, ValueError, "iterations must be at least 1"),
        ({"warmup": -1}, ValueError, "warmup must be zero or more"),
        ({"blocks": 0}, ValueError, "blocks must be at least 1"),
        ({"sections": 0}, ValueError, "sections must be at least 1"),
        ({"value_size": -1}, ValueError, "value_size must be zero or more"),
        ({"search_limit": -1}, ValueError, "search_limit must be zero or more"),
        ({"blocks": 4, "sections": 5}, ValueError, "sections must not be greater than blocks"),
        (
            {"blocks": 4, "sections": 2, "links_per_block": 5},
            ValueError,
            "links_per_block must not be greater",
        ),
        ({"iterations": True}, TypeError, "iterations must be an integer"),
        ({"seed": "abc"}, TypeError, "seed must be an integer"),
    ],
)
def test_settings_reject_counts_that_cannot_describe_a_run(
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """`BenchmarkSettings` rejects a bad size before a run starts, not during the run."""

    with pytest.raises(error, match=message):
        BenchmarkSettings(**kwargs)


def test_case_selection_filters_by_group_name_and_layer() -> None:
    """Case selection on the command line keeps the registry order and matches substrings."""

    cases = default_cases()

    assert [case.name for case in select(cases, groups=("link",))] == ["add_link", "list_links"]
    assert [case.name for case in select(cases, names=("delete",))] == [
        "delete_leaf",
        "delete_subtree",
    ]
    assert {case.name for case in select(cases, layers=(CORE,))} == {
        "codec_round_trip",
        "policy_check",
        "hook_dispatch",
    }
    assert select(cases, names=("no_such_case",)) == ()


def test_report_renders_as_a_table_and_round_trips_as_json(tmp_path: Path) -> None:
    """The table is for a person and the JSON file is for the next release."""

    cases = select(default_cases(), names=("get",))
    result = runner.run(settings=TINY, cases=cases, progress=_quiet())

    text = report.format_report(result)
    assert "Genie benchmarks" in text
    assert "in_memory" in text
    assert all(header in text for header in LATENCY_HEADERS)
    assert "overhead: what the Memory call adds" in text

    written = report.write_json(result, tmp_path / "nested" / "report.json")
    assert json.loads(written.read_text(encoding="utf-8")) == result


def test_the_concurrency_table_opens_each_layer_with_its_single_caller_row() -> None:
    """Each layer starts with its single-caller row, because the rows for more workers are only useful next to it."""

    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=TINY_CONCURRENT,
        targets=default_targets()[:1],
        cases=cases,
        layers=(BACKEND,),
        progress=_quiet(),
    )

    text = report.format_report(result)
    lines = text.splitlines()
    heading = lines.index("concurrency: how the same call behaves with more callers")
    rows = [line for line in lines[heading:] if line.startswith("get ")]

    assert all(header in text for header in CONCURRENCY_HEADERS)
    assert len(rows) == 2
    # case, layer, workers, p50, p95, wall ops/s, speedup, conflicts, errors
    assert [row.split()[2] for row in rows] == ["1", str(WORKERS)]
    assert rows[0].split()[6] == "1.00"


def test_progress_writes_whole_lines_when_the_stream_is_not_a_terminal() -> None:
    """A log cannot erase a line, so the reporter writes whole lines at long intervals."""

    stream = io.StringIO()
    progress = for_stream(stream)

    assert progress.rewrite is False
    assert progress.interval == PLAIN_INTERVAL_SECONDS

    progress.start_variant(1, 2, "sqlite backend: get")
    progress.call_done(2, 2)
    progress.finish_variant(2, 1.5, 0.25)
    progress.close()
    written = stream.getvalue()

    assert "\r" not in written
    assert "sqlite backend: get preparing" in written
    assert "2 calls in 1.5s  p50 0.250 ms" in written


def test_progress_updates_one_line_when_the_stream_is_a_terminal() -> None:
    """On a terminal, the reporter rewrites one line in place several times a second."""

    stream = _FakeTerminal()
    progress = for_stream(stream)

    assert progress.rewrite is True
    assert progress.interval == TERMINAL_INTERVAL_SECONDS

    progress.start_variant(1, 2, "in_memory memory: search_subtree_text")
    progress.call_done(1, 2)
    progress.call_done(2, 2)
    progress.finish_variant(2, 66.1, 300.126)
    progress.close()
    written = stream.getvalue()

    assert written.startswith("\r")
    assert written.count("\n") == 1
    assert "1/2 calls" in written
    assert written.rstrip().endswith("p50 300.126 ms")
    # Setup runs before the first timed call and takes most of the time of a
    # server variant. So the terminal shows that the variant is "preparing" and
    # does not stay empty.
    assert "preparing" in written.split("1/2 calls")[0]


def test_progress_writes_nothing_when_quiet() -> None:
    """The progress reporter writes nothing with `--quiet`, so `--quiet` together with `--json` leaves both output streams empty."""

    stream = io.StringIO()
    progress = for_stream(stream, quiet=True)

    progress.start_variant(1, 1, "in_memory backend: get")
    progress.call_done(1, 1)
    progress.finish_variant(1, 0.1, 0.01)
    progress.skip_target("postgres", "no server")
    progress.close()

    assert stream.getvalue() == ""


def test_context_for_a_core_case_writes_nothing_to_the_store() -> None:
    """A core helper needs no corpus, so its context writes nothing to the store."""

    case = next(item for item in default_cases() if item.name == "policy_check")
    item = WorkItem(CORE_SECTION, None, case, CORE)
    context, dispose = runner.build_context(item, TINY)
    try:
        assert isinstance(context, CaseContext)
        assert context.backend._blocks == {}
    finally:
        dispose()


def test_the_command_line_lists_targets_and_cases_without_measuring() -> None:
    """`--list` shows what a run will do and measures nothing."""

    assert run.main(["--list", "--quiet"]) == run.EXIT_OK


def test_an_unknown_target_is_refused_before_any_work_starts() -> None:
    """A name that matches nothing is a usage error, not an empty report."""

    assert run.main(["--target", "cassandra"]) == run.EXIT_BAD_SELECTION
    assert run.main(["--case", "no_such_case"]) == run.EXIT_BAD_SELECTION


def test_the_command_line_runs_a_single_case_and_writes_json(tmp_path: Path) -> None:
    """The smallest useful run has one case, one target, one layer, and only JSON output."""

    destination = tmp_path / "report.json"
    status = run.main(
        [
            "--case",
            "get",
            "--target",
            "in_memory",
            "--layer",
            "backend",
            "--iterations",
            "2",
            "--warmup",
            "1",
            "--blocks",
            "6",
            "--sections",
            "2",
            "--links-per-block",
            "2",
            "--quiet",
            "--json",
            str(destination),
        ]
    )
    written = json.loads(destination.read_text(encoding="utf-8"))

    assert status == run.EXIT_OK
    assert [section["name"] for section in written["sections"]] == ["in_memory"]
    assert [case["name"] for case in written["sections"][0]["cases"]] == ["get"]
    assert list(written["sections"][0]["cases"][0]["variants"]) == [BACKEND]


def test_the_command_line_measures_worker_levels(tmp_path: Path) -> None:
    """`--workers` alone enables the concurrency measurement, and the run adds level 1 automatically."""

    destination = tmp_path / "concurrent.json"
    status = run.main(
        [
            "--case",
            "get",
            "--target",
            "in_memory",
            "--layer",
            "backend",
            "--workers",
            str(WORKERS),
            "--iterations",
            "2",
            "--warmup",
            "1",
            "--blocks",
            "6",
            "--sections",
            "2",
            "--links-per-block",
            "2",
            "--quiet",
            "--json",
            str(destination),
        ]
    )
    written = json.loads(destination.read_text(encoding="utf-8"))
    entry = written["sections"][0]["cases"][0]

    assert status == run.EXIT_OK
    assert written["workers"] == [1, WORKERS]
    assert entry["variants"][BACKEND]["workers"] == 1
    assert [level["workers"] for level in entry["concurrency"]] == [WORKERS]


def test_the_command_line_says_why_a_contention_case_needs_workers() -> None:
    """The error names the flag that the contention cases need, which helps more than a message that nothing matched."""

    assert run.main(["--group", "contention", "--quiet"]) == run.EXIT_BAD_SELECTION


def test_the_command_line_refuses_a_worker_level_below_one() -> None:
    """Zero callers cannot run a case, so the command line rejects `--workers 0` before any work starts."""

    with pytest.raises(SystemExit) as raised:
        run.main(["--workers", "0"])

    assert raised.value.code == 2


def test_per_query_driver_notices_are_kept_out_of_a_measured_run() -> None:
    """If the driver writes a notice on every call, the run times that notice as part of the call.

    - **Neo4j notifications.** Neo4j reports a notification when a query names a
      label or property that is not yet in the database. Nearly every query
      against a store that a variant emptied a short time before does this.
    - **Cost in the timed call.** The driver logs the notification while it
      streams the result. It writes the log record during the timed call, onto
      the stream that the progress line also uses.
    """

    assert "neo4j.notifications" in run.NOISY_DRIVER_LOGGERS
    levels = {name: logging.getLogger(name).level for name in run.NOISY_DRIVER_LOGGERS}
    try:
        for name in run.NOISY_DRIVER_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)

        run.quiet_driver_logs()

        for name in run.NOISY_DRIVER_LOGGERS:
            logger = logging.getLogger(name)
            # If the logger is not enabled for a level, it never builds the record.
            # So the timed call does not include the cost of the record or of its output.
            assert not logger.isEnabledFor(logging.WARNING)
            assert logger.isEnabledFor(logging.ERROR)
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def test_the_command_line_refuses_settings_that_cannot_describe_a_run() -> None:
    """The argument parser rejects bad settings with an error and does not start a useless run."""

    with pytest.raises(SystemExit) as raised:
        run.main(["--iterations", "0"])

    assert raised.value.code == 2


# --------------------------------------------------------------------------
# mixed read/write workload
# --------------------------------------------------------------------------


TINY_WORKLOAD = BenchmarkSettings(
    warmup=1,
    blocks=120,
    sections=4,
    value_size=4,
    links_per_block=2,
    workers=(1, 4),
    workload=True,
)
"""Settings that are small enough for a unit test and large enough for a real mix of reads and writes.

- **Why the size matters.** The sweep lasts longer than a few thread switches.
  A shorter sweep can starve the readers of a mixed pass, so that they make no
  calls at all.
- **Related test.** `test_a_stream_that_recorded_nothing_still_reports` covers
  that case.
"""


def _target(name: str) -> BackendTarget:
    """Return one shipped target by name."""

    return next(target for target in default_targets() if target.name == name)


def _workload_report(name: str = "sqlite") -> dict[str, Any]:
    """Measure the mixed scenario on one target.

    - **Why `sqlite` is the default.** Its driver releases the interpreter
      lock, so the scheduler always runs its readers and its call counts repeat
      from run to run.

    Args:
        name: Target to measure.

    Returns:
        The report.
    """

    return runner.run(settings=TINY_WORKLOAD, targets=(_target(name),), progress=_quiet())


def _entry(result: dict[str, Any], workers: int = 4) -> dict[str, Any]:
    """Return one measured mixed variant from a report."""

    return next(
        entry
        for section in result["sections"]
        for entry in section["workloads"]
        if entry["workers"] == workers
    )


def _stream(entry: dict[str, Any], stream: str) -> dict[str, Any]:
    """Return one stream of one mixed variant."""

    return next(item for item in entry["streams"] if item["stream"] == stream)


def _block(memory: Memory, block_id: str) -> MemoryBlock:
    """Return a block that the test expects to exist.

    Args:
        memory: Memory to read from.
        block_id: Id of the block that the caller expects to exist.

    Returns:
        The block.
    """

    block = memory.get(block_id)
    assert block is not None, f"{block_id} should exist"
    return block


@pytest.fixture(scope="module")
def mixed_report() -> dict[str, Any]:
    """Return one mixed `sqlite` run for the tests that only read the report."""

    return _workload_report()


@pytest.mark.parametrize(
    ("workers", "expected"),
    [
        (2, (1, 1)),
        (3, (2, 1)),
        (4, (3, 1)),
        (5, (4, 1)),
        (6, (4, 2)),
        (8, (6, 2)),
        (12, (9, 3)),
        (16, (12, 4)),
    ],
)
def test_the_split_puts_three_readers_behind_every_writer(
    workers: int,
    expected: tuple[int, int],
) -> None:
    """The split gives exactly the 3 to 1 target ratio for the common worker counts."""

    roles = split_roles(workers)

    assert (roles.readers, roles.writers) == expected
    assert roles.workers == workers


def test_the_split_keeps_at_least_one_worker_in_each_stream() -> None:
    """Two workers cannot give a 3 to 1 ratio, so the split moves away from the ratio on purpose."""

    roles = split_roles(MINIMUM_WORKERS)

    assert roles.readers == 1
    assert roles.writers == 1
    assert roles.readers / roles.writers != READ_PER_WRITE


@pytest.mark.parametrize("workers", [1, 0, -3])
def test_the_split_refuses_a_worker_count_that_cannot_mix(workers: int) -> None:
    """One worker cannot read and write at the same time."""

    with pytest.raises(ValueError):
        split_roles(workers)


def test_readers_and_writers_are_read_off_the_worker_index() -> None:
    """Readers get the low indexes, so the worker index gives the role without a separate lookup."""

    roles = split_roles(8)

    assert [roles.stream_of(index) for index in range(8)] == [STREAM_READ] * 6 + [STREAM_WRITE] * 2
    assert [roles.position_of(index) for index in range(8)] == [0, 1, 2, 3, 4, 5, 0, 1]


@pytest.mark.parametrize("index", [-1, 8, 99])
def test_a_worker_index_outside_the_split_is_refused(index: int) -> None:
    """An index that no worker holds is a caller bug, so the split raises `ValueError` and gives no default."""

    roles = split_roles(8)

    with pytest.raises(ValueError):
        roles.stream_of(index)
    with pytest.raises(ValueError):
        roles.position_of(index)


def test_reader_offsets_space_the_readers_over_the_corpus() -> None:
    """The offsets put 3 readers one third of the corpus apart, not on the same page."""

    offsets = [read_offset(position, 3, 1000) for position in range(3)]

    assert offsets == [0, 333, 666]


@pytest.mark.parametrize("writers", [1, 2, 3, 7, 120])
def test_write_ranges_cover_every_block_exactly_once(writers: int) -> None:
    """One sweep rewrites the whole corpus for any division of the blocks among the writers."""

    blocks = 120
    ranges = [write_range(position, writers, blocks) for position in range(writers)]
    covered = [index for start, stop in ranges for index in range(start, stop)]

    assert covered == list(range(blocks))


def test_write_ranges_refuse_fewer_blocks_than_writers() -> None:
    """A writer with no range to sweep measures nothing, so `write_range` rejects fewer blocks than writers."""

    with pytest.raises(ValueError):
        write_range(0, 4, 3)


@pytest.mark.parametrize("position", [-1, 3])
def test_a_position_outside_its_own_stream_is_refused(position: int) -> None:
    """`read_offset` and `write_range` reject a position that their stream does not hold."""

    with pytest.raises(ValueError):
        read_offset(position, 3, 120)
    with pytest.raises(ValueError):
        write_range(position, 3, 120)


def test_a_reader_walks_past_the_end_of_the_corpus_and_wraps() -> None:
    """A reader loops until the run stops it, so it wraps to the start and always has an id to read."""

    item = WorkloadItem("in_memory", _target("in_memory"), 4, split_roles(4))
    context, dispose = runner.build_workload_context(item, TINY_WORKLOAD)
    try:
        reader = prepare_worker(replace(context, worker_index=0), item.roles)
        found = [reader.prepared.call(index) is not None for index in range(TINY_WORKLOAD.blocks + 5)]
    finally:
        dispose()

    assert reader.stream == STREAM_READ
    assert reader.measured_calls is None
    assert all(found)


def test_a_writer_rewrites_every_block_exactly_once() -> None:
    """The sweep is one pass, so every leaf ends at one version above its first version."""

    item = WorkloadItem("in_memory", _target("in_memory"), 4, split_roles(4))
    context, dispose = runner.build_workload_context(item, TINY_WORKLOAD)
    try:
        writer = prepare_worker(replace(context, worker_index=3), item.roles)
        sweep = writer.measured_calls
        assert sweep is not None and sweep == TINY_WORKLOAD.blocks
        for index in range(TINY_WORKLOAD.warmup + sweep):
            writer.prepared.call(index)
        versions = {_block(context.memory, leaf.id).version for leaf in context.corpus.leaves}
    finally:
        dispose()

    assert writer.stream == STREAM_WRITE
    assert versions == {2}


def test_writer_warmup_never_eats_into_the_sweep() -> None:
    """Warmup writes go to a separate throwaway block, so the sweep stays one pass over the corpus."""

    item = WorkloadItem("in_memory", _target("in_memory"), 4, split_roles(4))
    context, dispose = runner.build_workload_context(item, TINY_WORKLOAD)
    try:
        writer = prepare_worker(replace(context, worker_index=3), item.roles)
        for index in range(TINY_WORKLOAD.warmup):
            writer.prepared.call(index)
        throwaway = _block(context.memory, f"{WARMUP_BLOCK_PREFIX}_w000")
        untouched = {_block(context.memory, leaf.id).version for leaf in context.corpus.leaves}
    finally:
        dispose()

    assert throwaway.version == TINY_WORKLOAD.warmup + 1
    assert throwaway.parent_id is None
    assert untouched == {1}


def test_a_writer_expects_to_lose_a_race_and_a_reader_does_not() -> None:
    """A write can conflict, so the writer declares `MemoryConflict`, and a reader declares no conflict."""

    item = WorkloadItem("in_memory", _target("in_memory"), 4, split_roles(4))
    context, dispose = runner.build_workload_context(item, TINY_WORKLOAD)
    try:
        reader = prepare_worker(replace(context, worker_index=0), item.roles)
        writer = prepare_worker(replace(context, worker_index=3), item.roles)
    finally:
        dispose()

    assert writer.prepared.conflict_errors == (MemoryConflict,)
    assert reader.prepared.conflict_errors == ()


def test_a_mixed_run_needs_more_than_one_worker() -> None:
    """A single caller has no mixed form, so the settings reject it."""

    with pytest.raises(ValueError):
        BenchmarkSettings(workload=True)
    with pytest.raises(ValueError):
        BenchmarkSettings(workload=True, workers=(1,))


def test_a_mixed_run_needs_a_block_for_every_writer() -> None:
    """The settings reject fewer leaves than writers, because a writer with no leaf has nothing to sweep."""

    with pytest.raises(ValueError):
        BenchmarkSettings(workload=True, workers=(8,), blocks=1, sections=1, links_per_block=0)


@pytest.mark.parametrize("workload", [1, 0, "yes", None])
def test_workload_must_be_a_boolean(workload: Any) -> None:
    """`workload` is the one switch that changes the shape of a run, so it accepts only a boolean."""

    with pytest.raises(TypeError):
        BenchmarkSettings(workload=workload, workers=(4,))


def test_workload_levels_leave_out_the_single_caller() -> None:
    """Level 1 is always in `workers`, and never in a mixed run."""

    settings = BenchmarkSettings(workload=True, workers=(8, 4))

    assert settings.workers == (1, 4, 8)
    assert settings.workload_levels == (4, 8)


def test_a_mixed_run_plans_one_variant_per_target_and_level() -> None:
    """The plan measures every available target at every level that the run asks for."""

    targets = (_target("in_memory"), _target("sqlite"))
    items, skipped = runner.plan_workload(targets, (4, 8))

    assert skipped == []
    assert [(item.section, item.workers) for item in items] == [
        ("in_memory", 4),
        ("in_memory", 8),
        ("sqlite", 4),
        ("sqlite", 8),
    ]


def test_a_mixed_run_reports_a_target_it_could_not_measure() -> None:
    """The report names a target that cannot run and does not omit it.

    - **The test sets the skip reason.** It does not read the reason from the
      environment. A target from `default_targets()` is unavailable only if its
      `GENIE_*` connection variables are not set. If the test reads those
      variables, it passes on a plain run and fails under
      `lib/scripts/test_full.sh`, because that script configures a live server.
    """

    reason = "no connection variables in this test"
    result = runner.run(
        settings=TINY_WORKLOAD,
        targets=(_target("in_memory"), replace(_target("neo4j"), skip_reason=reason)),
        progress=_quiet(),
    )
    sections = {section["name"]: section for section in result["sections"]}

    assert sections["neo4j"]["skipped"] == reason
    assert sections["neo4j"]["workloads"] == []
    assert sections["in_memory"]["workloads"]


def test_a_mixed_run_measures_no_cases(mixed_report: dict[str, Any]) -> None:
    """The workload replaces the cases and does not run beside them."""

    assert mixed_report["layers"] == [MEMORY]
    assert mixed_report["workers"] == list(TINY_WORKLOAD.workload_levels)
    assert all(section["cases"] == [] for section in mixed_report["sections"])
    assert mixed_report["errors"] == 0


def test_the_write_stream_makes_exactly_one_sweep(mixed_report: dict[str, Any]) -> None:
    """For any number of writers, the write stream rewrites the corpus once."""

    for workers in TINY_WORKLOAD.workload_levels:
        entry = _entry(mixed_report, workers)
        write = _stream(entry, STREAM_WRITE)

        assert write["result"]["samples"] == TINY_WORKLOAD.blocks
        assert write["result"]["workers"] == entry["writers"]
        assert write["result"]["notes"]["version"] == 2


def test_both_streams_divide_the_same_window(mixed_report: dict[str, Any]) -> None:
    """Both streams use one shared clock, so the sum of the stream rates equals the pass rate."""

    entry = _entry(mixed_report)
    results = [stream["result"] for stream in entry["streams"]]
    calls = sum(result["samples"] for result in results)
    rates = sum(result["throughput_per_second"] for result in results)

    assert {result["wall_seconds"] for result in results} == {entry["windows"]["mixed"]}
    assert rates == pytest.approx(calls / entry["windows"]["mixed"])
    assert sum(stream["share_percent"] for stream in entry["streams"]) == pytest.approx(100.0)


def test_the_read_baseline_repeats_the_mixed_call_counts(mixed_report: dict[str, Any]) -> None:
    """The read baseline repeats the same reads with the same readers, and only the writers are different."""

    read = _stream(_entry(mixed_report), STREAM_READ)

    assert read["result"]["samples"] > 0
    assert read["baseline"]["samples"] == read["result"]["samples"]
    assert read["baseline"]["workers"] == read["result"]["workers"]


def test_every_stream_reports_the_gap_against_its_baseline(mixed_report: dict[str, Any]) -> None:
    """The delta is the cost of the two streams that run together, and this cost is what the run measures."""

    entry = _entry(mixed_report)
    for stream in entry["streams"]:
        expected = stream["result"]["p50_ms"] - stream["baseline"]["p50_ms"]

        assert stream["p50_delta_ms"] == pytest.approx(expected)
        assert stream["p50_delta_percent"] == pytest.approx(
            expected / stream["baseline"]["p50_ms"] * 100.0
        )


def test_the_three_windows_and_the_sweep_ratio_are_reported(mixed_report: dict[str, Any]) -> None:
    """The entry reports a positive time window for each pass and the sweep ratio of the mixed pass to the writes-alone pass.

    - **What the ratio shows.** `sweep_ratio` divides the mixed window by the
      writes-alone window. It shows how the readers change the time of one
      write sweep.
    """

    entry = _entry(mixed_report)
    windows = entry["windows"]

    assert set(windows) == set(runner.WORKLOAD_PASSES)
    assert all(value > 0 for value in windows.values())
    assert entry["sweep_ratio"] == pytest.approx(
        windows[runner.PASS_MIXED] / windows[runner.PASS_WRITES_ALONE]
    )


def test_a_stream_that_recorded_nothing_still_reports() -> None:
    """A starved stream reports zeros and does not divide by them.

    - **A real outcome.** A pure-Python backend can hold the interpreter lock
      for a whole short sweep. Then its readers make no calls at all.
    - **The entry handles it.** This outcome is real, so the workload entry
      reports it without an error.
    """

    empty = summarize([], workers=3, wall_seconds=0.5).as_dict()
    busy = summarize([1_000_000] * 4, workers=1, wall_seconds=0.5).as_dict()
    item = WorkloadItem("in_memory", _target("in_memory"), 4, split_roles(4))
    passes = {
        runner.PASS_MIXED: {"streams": {STREAM_READ: empty, STREAM_WRITE: busy}, "wall_seconds": 0.5, "setup_seconds": 0.1},
        runner.PASS_READS_ALONE: {"streams": {STREAM_READ: empty}, "wall_seconds": 0.2, "setup_seconds": 0.1},
        runner.PASS_WRITES_ALONE: {"streams": {STREAM_WRITE: busy}, "wall_seconds": 0.3, "setup_seconds": 0.1},
    }

    entry = runner._workload_entry(item, passes)
    read = _stream(entry, STREAM_READ)

    assert read["share_percent"] == 0.0
    assert read["p50_delta_ms"] is None
    assert read["p50_delta_percent"] is None
    assert report.format_report({"sections": [{"name": "x", "workloads": [entry]}]})


def test_the_workload_table_has_a_row_per_stream_and_a_total(mixed_report: dict[str, Any]) -> None:
    """The report has one table for each level, and a `total` row at the end sums only the columns that have a meaningful sum."""

    text = report.format_report(mixed_report)
    lines = text.splitlines()

    assert any(line.startswith("workload x4: 3 readers, 1 writer") for line in lines)
    assert any("one sweep of 120 blocks" in line for line in lines)
    assert sum(line.startswith("total") for line in lines) == len(TINY_WORKLOAD.workload_levels)
    header = next(line for line in lines if line.strip().startswith("stream"))
    assert all(column in header for column in WORKLOAD_HEADERS)
    total = next(line for line in lines if line.startswith("total"))
    assert total.split()[1] == "4"


def test_the_settings_line_leaves_out_what_a_mixed_run_never_reads(
    mixed_report: dict[str, Any],
) -> None:
    """The settings line omits each setting that a mixed run never reads, so no unused number looks like it has an effect."""

    line = next(
        line for line in report.format_report(mixed_report).splitlines()
        if line.startswith("settings:")
    )

    assert "workload=True" in line
    assert f"workers={list(TINY_WORKLOAD.workload_levels)}" in line
    for unused in report.WORKLOAD_UNUSED_SETTINGS:
        assert f"{unused}=" not in line
        assert unused in mixed_report["settings"]


def test_a_mixed_report_names_the_run_and_round_trips_as_json(
    mixed_report: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The header counts variants, not cases, because a mixed run has no cases."""

    text = report.format_report(mixed_report)
    destination = report.write_json(mixed_report, tmp_path / "mixed.json")

    assert "run: mixed workload, 1 variant in 1 section" in text
    # The old header counted cases. For this run, it showed "0 cases, 0 variants".
    assert "case" not in text.split("\n\n")[0]
    assert json.loads(destination.read_text(encoding="utf-8")) == mixed_report


def test_the_command_line_lists_a_mixed_run_without_measuring() -> None:
    """`--list` names the levels, the split, and the three passes."""

    assert run.main(["--workload", "--workers", "4", "--list", "--quiet"]) == run.EXIT_OK


def test_the_command_line_refuses_a_case_selection_with_the_workload() -> None:
    """A run with `--workload` has no cases, so the command line rejects any case selection."""

    for selection in (["--group", "read"], ["--layer", "backend"], ["--case", "get"]):
        assert run.main(["--workload", "--workers", "4", *selection]) == run.EXIT_BAD_SELECTION


def test_the_command_line_runs_the_workload_and_writes_json(tmp_path: Path) -> None:
    """The smallest useful mixed run has one target, one level, and only JSON output."""

    destination = tmp_path / "workload.json"
    status = run.main(
        [
            "--workload",
            "--workers", "4",
            "--target", "in_memory",
            "--blocks", "40",
            "--sections", "2",
            "--links-per-block", "2",
            "--value-size", "4",
            "--warmup", "1",
            "--quiet",
            "--json", str(destination),
        ]
    )
    written = json.loads(destination.read_text(encoding="utf-8"))
    entry = _entry(written)

    assert status == run.EXIT_OK
    assert written["settings"]["workload"] is True
    assert _stream(entry, STREAM_WRITE)["result"]["samples"] == 40


def test_a_pass_counts_calls_that_returned_not_calls_that_started() -> None:
    """The call count that a baseline inherits does not include failures.

    - **A failed call did no work.** A repeat of that call measures the failure
      path, not the operation.
    - **Termination.** A mixed reader has no call count of its own. If every
      read raises, the reader loops as fast as the interpreter allows. If the
      count includes attempts, the reads-alone pass gets millions of failures
      to repeat instead of zero calls.
    """

    def raises(_index: int) -> Any:
        raise RuntimeError("injected read failure")

    settings = replace(TINY_WORKLOAD, warmup=1)
    working = WorkloadWorker(STREAM_READ, 0, PreparedCase(call=lambda _index: "ok"), 4)
    broken = WorkloadWorker(STREAM_READ, 1, PreparedCase(call=raises), 5)

    result = runner._run_workload_pass(
        {0: working, 1: broken},
        caps=None,
        settings=settings,
        progress=None,
        setup_seconds=0.0,
        scale={},
    )

    assert result["worker_calls"][0] == 4
    assert result["worker_calls"][1] == 0
    assert result["streams"][STREAM_READ]["samples"] == 4
    assert result["streams"][STREAM_READ]["errors"] == 5
