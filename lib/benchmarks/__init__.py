"""Latency benchmarks for the raw memory operations of Genie.

- **Two layers:** the package measures the cost of one operation at 2 layers.
  The first layer is a raw backend protocol call. The second layer is the
  equivalent public `Memory` call. The gap between them is the cost of the
  policy check, the hook dispatch, the codec, and the block copy of Genie.
- **Concurrency:** `--workers` adds a second question. What changes when more
  than one caller shares one `Memory` and one backend? The report gives the
  wall-clock throughput together with the rate of each caller, the speedup
  against a single caller, and the races that a hot block loses.
- **No assertions:** `pytest` does not collect this directory, so a normal
  test run stays fast. `lib/tests/test_benchmarks.py` checks that the harness
  works.

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
