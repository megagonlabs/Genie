"""Latency benchmarks for Genie's raw memory operations.

The package measures what one operation costs at two layers: a raw backend
protocol call, and the matching public `Memory` call. The gap between them is
what Genie's own policy check, hook dispatch, codec, and block copying cost.

`--workers` adds a second question on top of that one: what changes when
several callers share one `Memory` and one backend. It reports wall-clock
throughput next to the per-caller rate, the speedup against a single caller,
and the races a hot block loses.

Nothing here asserts. `pytest` does not collect this directory, so a normal
test run stays fast. `lib/tests/test_benchmarks.py` keeps the harness working.

Run it from the repository root:

    python lib/benchmarks/run.py
"""

from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, SINGLE_CALLER, BenchmarkSettings

__all__ = [
    "BACKEND",
    "CORE",
    "LAYERS",
    "MEMORY",
    "SINGLE_CALLER",
    "BenchmarkSettings",
]
