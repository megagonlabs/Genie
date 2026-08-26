#!/usr/bin/env bash

# Run Genie's complete test suite against disposable local Neo4j and Redis
# servers.

set -euo pipefail

GENIE_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
GENIE_TEST_CONDA_ENV="${GENIE_TEST_CONDA_ENV:-genie}"
GENIE_TEST_NEO4J_IMAGE="${GENIE_TEST_NEO4J_IMAGE:-neo4j:5-community}"
GENIE_TEST_NEO4J_CONTAINER="${GENIE_TEST_NEO4J_CONTAINER:-genie-neo4j-test}"
GENIE_TEST_NEO4J_HOST_PORT="${GENIE_TEST_NEO4J_HOST_PORT:-17687}"
GENIE_TEST_NEO4J_USER="${GENIE_TEST_NEO4J_USER:-neo4j}"
GENIE_TEST_NEO4J_PASSWORD="${GENIE_TEST_NEO4J_PASSWORD:-genie-test-password}"
GENIE_TEST_NEO4J_DATABASE="${GENIE_TEST_NEO4J_DATABASE:-neo4j}"
GENIE_TEST_NEO4J_READY_ATTEMPTS="${GENIE_TEST_NEO4J_READY_ATTEMPTS:-60}"
GENIE_TEST_REDIS_IMAGE="${GENIE_TEST_REDIS_IMAGE:-redis:7-alpine}"
GENIE_TEST_REDIS_CONTAINER="${GENIE_TEST_REDIS_CONTAINER:-genie-redis-test}"
GENIE_TEST_REDIS_HOST_PORT="${GENIE_TEST_REDIS_HOST_PORT:-16379}"
GENIE_TEST_REDIS_DATABASE="${GENIE_TEST_REDIS_DATABASE:-0}"
GENIE_TEST_REDIS_KEY_PREFIX="${GENIE_TEST_REDIS_KEY_PREFIX:-genie-test}"
GENIE_TEST_REDIS_READY_ATTEMPTS="${GENIE_TEST_REDIS_READY_ATTEMPTS:-60}"

cd "$GENIE_REPO_ROOT"

require_command() {
    local command_name="$1"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command '$command_name' was not found." >&2
        exit 1
    fi
}

require_port() {
    local name="$1"
    local value="$2"
    if ! [[ "$value" =~ ^[0-9]+$ ]] || ((value < 1 || value > 65535)); then
        echo "$name must be an integer from 1 through 65535." >&2
        exit 1
    fi
}

require_attempts() {
    local name="$1"
    local value="$2"
    if ! [[ "$value" =~ ^[0-9]+$ ]] || ((value < 1)); then
        echo "$name must be a positive integer." >&2
        exit 1
    fi
}

require_free_container() {
    local container_name="$1"
    if docker inspect "$container_name" >/dev/null 2>&1; then
        echo "Docker container '$container_name' already exists." >&2
        echo "Stop or rename it before running this script." >&2
        exit 1
    fi
}

cleanup() {
    if [[ "${GENIE_TEST_NEO4J_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Neo4j container..."
        docker stop "$GENIE_TEST_NEO4J_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ "${GENIE_TEST_REDIS_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Redis container..."
        docker stop "$GENIE_TEST_REDIS_CONTAINER" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_command conda
require_command docker

require_port GENIE_TEST_NEO4J_HOST_PORT "$GENIE_TEST_NEO4J_HOST_PORT"
require_port GENIE_TEST_REDIS_HOST_PORT "$GENIE_TEST_REDIS_HOST_PORT"
require_attempts GENIE_TEST_NEO4J_READY_ATTEMPTS "$GENIE_TEST_NEO4J_READY_ATTEMPTS"
require_attempts GENIE_TEST_REDIS_READY_ATTEMPTS "$GENIE_TEST_REDIS_READY_ATTEMPTS"

if ! conda run -n "$GENIE_TEST_CONDA_ENV" python -c "import neo4j" >/dev/null 2>&1; then
    echo "The Neo4j test dependency is missing from Conda environment '$GENIE_TEST_CONDA_ENV'." >&2
    echo "Install it with: conda run -n \"$GENIE_TEST_CONDA_ENV\" python -m pip install -e \".[dev,neo4j,redis]\"" >&2
    exit 1
fi

if ! conda run -n "$GENIE_TEST_CONDA_ENV" python -c "import redis" >/dev/null 2>&1; then
    echo "The Redis test dependency is missing from Conda environment '$GENIE_TEST_CONDA_ENV'." >&2
    echo "Install it with: conda run -n \"$GENIE_TEST_CONDA_ENV\" python -m pip install -e \".[dev,neo4j,redis]\"" >&2
    exit 1
fi

require_free_container "$GENIE_TEST_NEO4J_CONTAINER"
require_free_container "$GENIE_TEST_REDIS_CONTAINER"

echo "Compiling Genie..."
conda run -n "$GENIE_TEST_CONDA_ENV" python -m compileall -q genie tests examples lamps

echo "Starting disposable Neo4j container..."
docker run --rm -d \
    --name "$GENIE_TEST_NEO4J_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}:7687" \
    -e "NEO4J_AUTH=${GENIE_TEST_NEO4J_USER}/${GENIE_TEST_NEO4J_PASSWORD}" \
    "$GENIE_TEST_NEO4J_IMAGE" >/dev/null
GENIE_TEST_NEO4J_STARTED=true

echo "Starting disposable Redis container..."
docker run --rm -d \
    --name "$GENIE_TEST_REDIS_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_REDIS_HOST_PORT}:6379" \
    "$GENIE_TEST_REDIS_IMAGE" >/dev/null
GENIE_TEST_REDIS_STARTED=true

echo "Waiting for Redis..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_REDIS_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    if docker exec "$GENIE_TEST_REDIS_CONTAINER" redis-cli PING >/dev/null 2>&1; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_REDIS_READY_ATTEMPTS)); then
        echo "Redis did not become ready. Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_REDIS_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "Waiting for Neo4j..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_NEO4J_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    if docker exec "$GENIE_TEST_NEO4J_CONTAINER" \
        cypher-shell \
        -u "$GENIE_TEST_NEO4J_USER" \
        -p "$GENIE_TEST_NEO4J_PASSWORD" \
        -d "$GENIE_TEST_NEO4J_DATABASE" \
        "RETURN 1;" >/dev/null 2>&1; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_NEO4J_READY_ATTEMPTS)); then
        echo "Neo4j did not become ready. Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_NEO4J_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "Running the complete Genie test suite with live Neo4j and Redis tests enabled..."
env \
    GENIE_NEO4J_URI="bolt://127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}" \
    GENIE_NEO4J_USER="$GENIE_TEST_NEO4J_USER" \
    GENIE_NEO4J_PASSWORD="$GENIE_TEST_NEO4J_PASSWORD" \
    GENIE_NEO4J_DATABASE="$GENIE_TEST_NEO4J_DATABASE" \
    GENIE_REDIS_URL="redis://127.0.0.1:${GENIE_TEST_REDIS_HOST_PORT}/${GENIE_TEST_REDIS_DATABASE}" \
    GENIE_REDIS_KEY_PREFIX="$GENIE_TEST_REDIS_KEY_PREFIX" \
    conda run -n "$GENIE_TEST_CONDA_ENV" python -m pytest "$@"
