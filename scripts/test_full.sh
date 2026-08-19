#!/usr/bin/env bash

# Run Genie's complete test suite against a disposable local Neo4j server.

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

cd "$GENIE_REPO_ROOT"

require_command() {
    local command_name="$1"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command '$command_name' was not found." >&2
        exit 1
    fi
}

cleanup() {
    if [[ "${GENIE_TEST_CONTAINER_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Neo4j container..."
        docker stop "$GENIE_TEST_NEO4J_CONTAINER" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_command conda
require_command docker

if ! [[ "$GENIE_TEST_NEO4J_HOST_PORT" =~ ^[0-9]+$ ]] || ((GENIE_TEST_NEO4J_HOST_PORT < 1 || GENIE_TEST_NEO4J_HOST_PORT > 65535)); then
    echo "GENIE_TEST_NEO4J_HOST_PORT must be an integer from 1 through 65535." >&2
    exit 1
fi
if ! [[ "$GENIE_TEST_NEO4J_READY_ATTEMPTS" =~ ^[0-9]+$ ]] || ((GENIE_TEST_NEO4J_READY_ATTEMPTS < 1)); then
    echo "GENIE_TEST_NEO4J_READY_ATTEMPTS must be a positive integer." >&2
    exit 1
fi

if ! conda run -n "$GENIE_TEST_CONDA_ENV" python -c "import neo4j" >/dev/null 2>&1; then
    echo "The Neo4j test dependency is missing from Conda environment '$GENIE_TEST_CONDA_ENV'." >&2
    echo "Install it with: conda run -n \"$GENIE_TEST_CONDA_ENV\" python -m pip install -e \".[dev,neo4j]\"" >&2
    exit 1
fi

if docker inspect "$GENIE_TEST_NEO4J_CONTAINER" >/dev/null 2>&1; then
    echo "Docker container '$GENIE_TEST_NEO4J_CONTAINER' already exists." >&2
    echo "Stop or rename it before running this script." >&2
    exit 1
fi

echo "Compiling Genie..."
conda run -n "$GENIE_TEST_CONDA_ENV" python -m compileall -q genie tests examples lamps

echo "Starting disposable Neo4j container..."
docker run --rm -d \
    --name "$GENIE_TEST_NEO4J_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}:7687" \
    -e "NEO4J_AUTH=${GENIE_TEST_NEO4J_USER}/${GENIE_TEST_NEO4J_PASSWORD}" \
    "$GENIE_TEST_NEO4J_IMAGE" >/dev/null
GENIE_TEST_CONTAINER_STARTED=true

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

echo "Running the complete Genie test suite with live Neo4j tests enabled..."
env \
    GENIE_NEO4J_URI="bolt://127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}" \
    GENIE_NEO4J_USER="$GENIE_TEST_NEO4J_USER" \
    GENIE_NEO4J_PASSWORD="$GENIE_TEST_NEO4J_PASSWORD" \
    GENIE_NEO4J_DATABASE="$GENIE_TEST_NEO4J_DATABASE" \
    conda run -n "$GENIE_TEST_CONDA_ENV" python -m pytest "$@"
