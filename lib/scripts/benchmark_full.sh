#!/usr/bin/env bash

# Run the Genie benchmarks against disposable local PostgreSQL, Neo4j, MongoDB,
# and Redis servers. So one report can compare the 6 benchmark targets.
#
# This script does not repeat the container setup. `test_full.sh` already
# starts the servers, waits for them, and disposes of them. So this script only
# tells `test_full.sh` to run the benchmarks instead of pytest. The script gives
# extra arguments to the benchmark runner:
#
#   ./lib/scripts/benchmark_full.sh
#   ./lib/scripts/benchmark_full.sh --iterations 50 --blocks 500
#   ./lib/scripts/benchmark_full.sh --group read --json reports/servers.json
#   ./lib/scripts/benchmark_full.sh --group contention --workers 4 --blocks 200
#
# A run on 6 targets takes a long time. A server call costs much more than an
# in-process call, and each variant writes its corpus again. During
# development, use lower `--iterations` and `--blocks` values. Keep the
# defaults for a report that you will compare with later reports.
#
# Only a server can show a speedup above 1 for the pooled adapters. So here,
# `--workers` has value for the extra time. Locally, it does not.

set -euo pipefail

GENIE_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

export GENIE_TEST_COMMAND="${GENIE_TEST_PYTHON:-python} lib/benchmarks/run.py"
export GENIE_TEST_LABEL="the Genie benchmarks"

# A benchmark variant empties its store before it writes. So the benchmarks
# never share a namespace with the live tests. These defaults keep them
# separate, also when both use the same server. Neo4j has no namespace default
# here. Its own container below keeps it separate.
export GENIE_TEST_POSTGRES_SCHEMA="${GENIE_TEST_POSTGRES_SCHEMA:-genie_benchmark}"
export GENIE_TEST_MONGODB_DATABASE="${GENIE_TEST_MONGODB_DATABASE:-genie_benchmark}"
export GENIE_TEST_REDIS_KEY_PREFIX="${GENIE_TEST_REDIS_KEY_PREFIX:-genie-benchmark}"

# Separate container names and ports. So a benchmark run and a test run can run
# at the same time, and neither run refuses to start.
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
