#!/usr/bin/env bash

# Run the Genie benchmarks against disposable local PostgreSQL, Neo4j, MongoDB,
# and Redis servers, so the six shipped backends can be compared in one report.
#
# The container setup is not repeated here. `test_full.sh` already starts,
# waits for, and disposes of every server, so this script only tells it what to
# run instead of pytest. Extra arguments are passed on to the benchmark runner:
#
#   ./lib/scripts/benchmark_full.sh
#   ./lib/scripts/benchmark_full.sh --iterations 50 --blocks 500
#   ./lib/scripts/benchmark_full.sh --group read --json reports/servers.json
#   ./lib/scripts/benchmark_full.sh --group contention --workers 4 --blocks 200
#
# A six-target run is long, because a server call costs far more than an
# in-process one and every variant rewrites its corpus. Lower `--iterations` and
# `--blocks` while you are working, and keep the defaults for a report you
# intend to compare against later.
#
# A server is also the only place the pooled adapters can show a speedup above
# 1, so `--workers` is worth the extra time here in a way it is not locally.

set -euo pipefail

GENIE_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

export GENIE_TEST_COMMAND="${GENIE_TEST_PYTHON:-python} lib/benchmarks/run.py"
export GENIE_TEST_LABEL="the Genie benchmarks"

# A benchmark variant empties its store before it writes, so the benchmarks must
# never share a namespace with the live tests. These defaults keep them apart
# even when both use the same server.
export GENIE_TEST_POSTGRES_SCHEMA="${GENIE_TEST_POSTGRES_SCHEMA:-genie_benchmark}"
export GENIE_TEST_MONGODB_DATABASE="${GENIE_TEST_MONGODB_DATABASE:-genie_benchmark}"
export GENIE_TEST_REDIS_KEY_PREFIX="${GENIE_TEST_REDIS_KEY_PREFIX:-genie-benchmark}"

# Separate container names and ports, so a benchmark run and a test run can be
# in flight at the same time without either refusing to start.
export GENIE_TEST_POSTGRES_CONTAINER="${GENIE_TEST_POSTGRES_CONTAINER:-genie-postgres-benchmark}"
export GENIE_TEST_POSTGRES_HOST_PORT="${GENIE_TEST_POSTGRES_HOST_PORT:-15433}"
export GENIE_TEST_NEO4J_CONTAINER="${GENIE_TEST_NEO4J_CONTAINER:-genie-neo4j-benchmark}"
export GENIE_TEST_NEO4J_HOST_PORT="${GENIE_TEST_NEO4J_HOST_PORT:-17688}"
export GENIE_TEST_MONGODB_CONTAINER="${GENIE_TEST_MONGODB_CONTAINER:-genie-mongodb-benchmark}"
export GENIE_TEST_MONGODB_HOST_PORT="${GENIE_TEST_MONGODB_HOST_PORT:-17018}"
export GENIE_TEST_REDIS_CONTAINER="${GENIE_TEST_REDIS_CONTAINER:-genie-redis-benchmark}"
export GENIE_TEST_REDIS_HOST_PORT="${GENIE_TEST_REDIS_HOST_PORT:-16381}"
export GENIE_TEST_REDIS_REPLICA_CONTAINER="${GENIE_TEST_REDIS_REPLICA_CONTAINER:-genie-redis-replica-benchmark}"
export GENIE_TEST_REDIS_REPLICA_HOST_PORT="${GENIE_TEST_REDIS_REPLICA_HOST_PORT:-16382}"

exec "$GENIE_REPO_ROOT/lib/scripts/test_full.sh" "$@"
