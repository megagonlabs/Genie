"""Latency benchmarks for Genie's raw memory operations.

The package measures what one operation costs at two layers: a raw backend
protocol call, and the matching public `Memory` call. The gap between them is
what Genie's own policy check, hook dispatch, codec, and block copying cost.

Nothing here asserts. `pytest` does not collect this directory, so a normal
test run stays fast. `lib/tests/test_benchmarks.py` keeps the harness working.

Run it from the repository root:

    conda run -n "genie" python lib/benchmarks/run.py
"""

from benchmarks.settings import BACKEND, CORE, LAYERS, MEMORY, BenchmarkSettings

__all__ = [
    "BACKEND",
    "CORE",
    "LAYERS",
    "MEMORY",
    "BenchmarkSettings",
]
