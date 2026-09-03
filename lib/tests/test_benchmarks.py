"""Tests for the benchmark harness under `lib/benchmarks/`.

The harness itself is not a test suite and nothing in it asserts, so these
tests keep it working. They run with tiny settings, so the suite stays fast.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks import report, run, runner
from benchmarks.cases import CaseContext, PreparedCase, default_cases, select
from benchmarks.metrics import percentile, summarize
from benchmarks.progress import PLAIN_INTERVAL_SECONDS, TERMINAL_INTERVAL_SECONDS, for_stream
from benchmarks.report import LATENCY_DROP_ORDER
from benchmarks.runner import CORE_SECTION, REPORT_SCHEMA, WorkItem
from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, BenchmarkSettings
from benchmarks.targets import BackendTarget, default_targets

TINY = BenchmarkSettings(
    iterations=2,
    warmup=1,
    blocks=6,
    sections=2,
    value_size=4,
    links_per_block=2,
    search_limit=3,
)
"""Settings small enough to run the whole harness inside a unit test."""


def _quiet() -> Any:
    """Return a progress reporter that writes nothing."""

    return for_stream(io.StringIO(), quiet=True)


class _FakeTerminal(io.StringIO):
    """Stream that claims to be a terminal, so progress rewrites one line."""

    def isatty(self) -> bool:
        """Report a terminal, whatever the real stream is."""

        return True


def test_every_case_runs_at_every_layer_it_declares() -> None:
    """A full run measures each case at each of its layers, with no errors."""

    result = runner.run(settings=TINY, progress=_quiet())

    assert result["schema"] == REPORT_SCHEMA
    assert result["errors"] == 0
    measured = {
        (section["name"], case["name"], layer)
        for section in result["sections"]
        for case in section["cases"]
        for layer in case["variants"]
    }
    for target in default_targets():
        for case in default_cases():
            for layer in (BACKEND, MEMORY):
                if case.prepare_for(layer) is not None:
                    assert (target.name, case.name, layer) in measured
    for case in default_cases():
        if case.prepare_for(CORE) is not None:
            assert (CORE_SECTION, case.name, CORE) in measured


def test_every_variant_reports_samples_and_the_scale_it_ran_against() -> None:
    """Each variant reports measured calls, and store cases report the scale."""

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
    """The gap between layers is honest only when both wrote the same data."""

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
    """A short result page is visible next to the budget that produced it."""

    cases = select(default_cases(), names=("search",))
    result = runner.run(settings=TINY, cases=cases, progress=_quiet())

    for section in result["sections"]:
        for case in section["cases"]:
            for variant in case["variants"].values():
                assert variant["notes"]["candidate_budget"] == TINY.search_limit
                assert variant["notes"]["returned"] <= TINY.search_limit


def test_a_paired_case_reports_the_layer_gap_and_a_single_layer_case_does_not() -> None:
    """Only a case measured at both layers can say what `Memory` added."""

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
    """A core helper has no store, so repeating it per target says nothing new."""

    cases = select(default_cases(), groups=("overhead",))
    items, skipped = runner.plan_run(default_targets(), cases, LAYERS)

    assert not skipped
    assert {item.section for item in items} == {CORE_SECTION}
    assert len(items) == len(cases)


def test_a_skipped_target_reports_its_reason_and_runs_no_case() -> None:
    """A target that cannot run is reported as skipped, never as a pass."""

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


def test_a_failing_call_is_counted_and_not_timed() -> None:
    """One exception must never be mistaken for a very fast call."""

    def call(index: int) -> Any:
        raise RuntimeError(f"call {index} refused")

    summary = runner.measure(PreparedCase(call=call), settings=TINY)

    assert summary.samples == 0
    assert summary.errors == TINY.iterations
    assert summary.first_error == f"RuntimeError: call {TINY.warmup} refused"
    assert summary.p50_ms == 0.0
    assert summary.ops_per_second == 0.0


def test_only_measured_calls_are_counted() -> None:
    """Warmup calls are made and thrown away, failures included."""

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
    """A case explains what its calls did, so a number can be read in context."""

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
    """A variant where nothing was timed still reports rather than crashing."""

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
    """Bad sizes fail before a run starts, not halfway through one."""

    with pytest.raises(error, match=message):
        BenchmarkSettings(**kwargs)


def test_case_selection_filters_by_group_name_and_layer() -> None:
    """Command line selection keeps registry order and matches on substrings."""

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
    assert "p50 ms" in text
    assert "overhead: what the Memory call adds" in text

    written = report.write_json(result, tmp_path / "nested" / "report.json")
    assert json.loads(written.read_text(encoding="utf-8")) == result


def _latency_headers(text: str) -> list[str]:
    """Return the header row of every latency table in a rendered report."""

    return [line for line in text.splitlines() if line.startswith("case ") and "layer" in line]


def test_a_narrow_width_drops_the_least_useful_column_first() -> None:
    """A narrow window loses `p99 ms` before `p95 ms`, and says what it lost."""

    cases = select(default_cases(), names=("get",))
    result = runner.run(
        settings=TINY,
        targets=default_targets()[:1],
        cases=cases,
        progress=_quiet(),
    )

    full = report.format_report(result, width=0)
    assert all("p99 ms" in line and "p95 ms" in line for line in _latency_headers(full))
    assert "left out to fit the width" not in full

    # One character short of fitting, so only the first column in the drop order
    # has to go.
    natural_width = len(_latency_headers(full)[0])
    medium = report.format_report(result, width=natural_width - 1)
    assert LATENCY_DROP_ORDER == ("p99 ms", "p95 ms")
    assert all("p99 ms" not in line and "p95 ms" in line for line in _latency_headers(medium))
    assert "left out to fit the width: p99 ms" in medium

    # Narrower still: both latency percentiles go, and the overhead table gives
    # up its share column too, because the two p50 columns already carry it.
    narrow = report.format_report(result, width=40)
    assert all("p99 ms" not in line and "p95 ms" not in line for line in _latency_headers(narrow))
    assert "left out to fit the width: +%, p95 ms, p99 ms" in narrow


def test_progress_writes_whole_lines_when_the_stream_is_not_a_terminal() -> None:
    """Nothing can erase a line in a log, so whole lines are written far apart."""

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
    assert "sqlite backend: get started" in written
    assert "2 calls in 1.5s  p50 0.250 ms" in written


def test_progress_updates_one_line_when_the_stream_is_a_terminal() -> None:
    """A terminal gets one line rewritten in place, several times a second."""

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


def test_progress_writes_nothing_when_quiet() -> None:
    """`--quiet` together with `--json` must leave both streams clean."""

    stream = io.StringIO()
    progress = for_stream(stream, quiet=True)

    progress.start_variant(1, 1, "in_memory backend: get")
    progress.call_done(1, 1)
    progress.finish_variant(1, 0.1, 0.01)
    progress.skip_target("postgres", "no server")
    progress.close()

    assert stream.getvalue() == ""


def test_context_for_a_core_case_writes_nothing_to_the_store() -> None:
    """A core helper needs no corpus, so it never pays to write one."""

    case = next(item for item in default_cases() if item.name == "policy_check")
    item = WorkItem(CORE_SECTION, None, case, CORE)
    context, dispose = runner.build_context(item, TINY)
    try:
        assert isinstance(context, CaseContext)
        assert context.backend._blocks == {}
    finally:
        dispose()


def test_the_command_line_lists_targets_and_cases_without_measuring() -> None:
    """`--list` is the cheap way to see what a run would do."""

    assert run.main(["--list", "--quiet"]) == run.EXIT_OK


def test_an_unknown_target_is_refused_before_any_work_starts() -> None:
    """A name that matches nothing is a usage error, not an empty report."""

    assert run.main(["--target", "cassandra"]) == run.EXIT_BAD_SELECTION
    assert run.main(["--case", "no_such_case"]) == run.EXIT_BAD_SELECTION


def test_the_command_line_runs_a_single_case_and_writes_json(tmp_path: Path) -> None:
    """The narrowest useful run: one case, one target, one layer, JSON only."""

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


def test_the_command_line_refuses_settings_that_cannot_describe_a_run() -> None:
    """Argument parsing fails loudly rather than starting a useless run."""

    with pytest.raises(SystemExit) as raised:
        run.main(["--iterations", "0"])

    assert raised.value.code == 2
