#!/usr/bin/env bash

# Run Genie's complete test suite against disposable local PostgreSQL, Neo4j,
# MongoDB, and Redis servers. The Redis side starts a primary and one replica that
# follows it, so the live tests can cover the adapter's replica role check as well.
# MongoDB starts as a single-member replica set, because every Genie write is one
# multi-document transaction and a standalone server cannot run those.
#
# `GENIE_TEST_COMMAND` chooses what runs once the servers are ready, so the same
# container setup can serve something other than pytest. `benchmark_full.sh` uses
# it to run the benchmarks. Extra arguments are passed on to that command.
#
# The command runs on the Python interpreter already on `PATH`, so activate your
# environment first. `GENIE_TEST_PYTHON` overrides that interpreter when you would
# rather not activate one. Output streams live, so a long run keeps reporting.

set -euo pipefail

GENIE_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
GENIE_TEST_PYTHON="${GENIE_TEST_PYTHON:-python}"
GENIE_TEST_COMMAND="${GENIE_TEST_COMMAND:-$GENIE_TEST_PYTHON -m pytest}"
GENIE_TEST_LABEL="${GENIE_TEST_LABEL:-the complete Genie test suite}"
GENIE_TEST_POSTGRES_IMAGE="${GENIE_TEST_POSTGRES_IMAGE:-postgres:16-alpine}"
GENIE_TEST_POSTGRES_CONTAINER="${GENIE_TEST_POSTGRES_CONTAINER:-genie-postgres-test}"
GENIE_TEST_POSTGRES_HOST_PORT="${GENIE_TEST_POSTGRES_HOST_PORT:-15432}"
GENIE_TEST_POSTGRES_USER="${GENIE_TEST_POSTGRES_USER:-genie}"
GENIE_TEST_POSTGRES_PASSWORD="${GENIE_TEST_POSTGRES_PASSWORD:-genie-test-password}"
GENIE_TEST_POSTGRES_DATABASE="${GENIE_TEST_POSTGRES_DATABASE:-genie}"
GENIE_TEST_POSTGRES_SCHEMA="${GENIE_TEST_POSTGRES_SCHEMA:-genie_test}"
GENIE_TEST_POSTGRES_READY_ATTEMPTS="${GENIE_TEST_POSTGRES_READY_ATTEMPTS:-60}"
GENIE_TEST_NEO4J_IMAGE="${GENIE_TEST_NEO4J_IMAGE:-neo4j:5-community}"
GENIE_TEST_NEO4J_CONTAINER="${GENIE_TEST_NEO4J_CONTAINER:-genie-neo4j-test}"
GENIE_TEST_NEO4J_HOST_PORT="${GENIE_TEST_NEO4J_HOST_PORT:-17687}"
GENIE_TEST_NEO4J_USER="${GENIE_TEST_NEO4J_USER:-neo4j}"
GENIE_TEST_NEO4J_PASSWORD="${GENIE_TEST_NEO4J_PASSWORD:-genie-test-password}"
GENIE_TEST_NEO4J_DATABASE="${GENIE_TEST_NEO4J_DATABASE:-neo4j}"
GENIE_TEST_NEO4J_READY_ATTEMPTS="${GENIE_TEST_NEO4J_READY_ATTEMPTS:-60}"
GENIE_TEST_MONGODB_IMAGE="${GENIE_TEST_MONGODB_IMAGE:-mongo:7}"
GENIE_TEST_MONGODB_CONTAINER="${GENIE_TEST_MONGODB_CONTAINER:-genie-mongodb-test}"
GENIE_TEST_MONGODB_HOST_PORT="${GENIE_TEST_MONGODB_HOST_PORT:-17017}"
GENIE_TEST_MONGODB_DATABASE="${GENIE_TEST_MONGODB_DATABASE:-genie_test}"
GENIE_TEST_MONGODB_REPLICA_SET="${GENIE_TEST_MONGODB_REPLICA_SET:-rs0}"
GENIE_TEST_MONGODB_READY_ATTEMPTS="${GENIE_TEST_MONGODB_READY_ATTEMPTS:-60}"
GENIE_TEST_REDIS_IMAGE="${GENIE_TEST_REDIS_IMAGE:-redis:7-alpine}"
GENIE_TEST_REDIS_CONTAINER="${GENIE_TEST_REDIS_CONTAINER:-genie-redis-test}"
GENIE_TEST_REDIS_HOST_PORT="${GENIE_TEST_REDIS_HOST_PORT:-16379}"
GENIE_TEST_REDIS_DATABASE="${GENIE_TEST_REDIS_DATABASE:-0}"
GENIE_TEST_REDIS_KEY_PREFIX="${GENIE_TEST_REDIS_KEY_PREFIX:-genie-test}"
GENIE_TEST_REDIS_READY_ATTEMPTS="${GENIE_TEST_REDIS_READY_ATTEMPTS:-60}"
GENIE_TEST_REDIS_REPLICA_CONTAINER="${GENIE_TEST_REDIS_REPLICA_CONTAINER:-genie-redis-replica-test}"
GENIE_TEST_REDIS_REPLICA_HOST_PORT="${GENIE_TEST_REDIS_REPLICA_HOST_PORT:-16380}"

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
    if [[ "${GENIE_TEST_POSTGRES_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable PostgreSQL container..."
        docker stop "$GENIE_TEST_POSTGRES_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ "${GENIE_TEST_NEO4J_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Neo4j container..."
        docker stop "$GENIE_TEST_NEO4J_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ "${GENIE_TEST_MONGODB_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable MongoDB container..."
        docker stop "$GENIE_TEST_MONGODB_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ "${GENIE_TEST_REDIS_REPLICA_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Redis replica container..."
        docker stop "$GENIE_TEST_REDIS_REPLICA_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ "${GENIE_TEST_REDIS_STARTED:-false}" == "true" ]]; then
        echo "Stopping disposable Redis container..."
        docker stop "$GENIE_TEST_REDIS_CONTAINER" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_command "$GENIE_TEST_PYTHON"
require_command docker

require_port GENIE_TEST_POSTGRES_HOST_PORT "$GENIE_TEST_POSTGRES_HOST_PORT"
require_port GENIE_TEST_NEO4J_HOST_PORT "$GENIE_TEST_NEO4J_HOST_PORT"
require_port GENIE_TEST_MONGODB_HOST_PORT "$GENIE_TEST_MONGODB_HOST_PORT"
require_port GENIE_TEST_REDIS_HOST_PORT "$GENIE_TEST_REDIS_HOST_PORT"
require_port GENIE_TEST_REDIS_REPLICA_HOST_PORT "$GENIE_TEST_REDIS_REPLICA_HOST_PORT"
require_attempts GENIE_TEST_POSTGRES_READY_ATTEMPTS "$GENIE_TEST_POSTGRES_READY_ATTEMPTS"
require_attempts GENIE_TEST_NEO4J_READY_ATTEMPTS "$GENIE_TEST_NEO4J_READY_ATTEMPTS"
require_attempts GENIE_TEST_MONGODB_READY_ATTEMPTS "$GENIE_TEST_MONGODB_READY_ATTEMPTS"
require_attempts GENIE_TEST_REDIS_READY_ATTEMPTS "$GENIE_TEST_REDIS_READY_ATTEMPTS"

if ! "$GENIE_TEST_PYTHON" -c "import psycopg" >/dev/null 2>&1; then
    echo "The PostgreSQL test dependency is missing from the active Python environment." >&2
    echo "Install it with: python -m pip install -e \".[dev,mongodb,neo4j,postgres,redis]\"" >&2
    exit 1
fi

if ! "$GENIE_TEST_PYTHON" -c "import neo4j" >/dev/null 2>&1; then
    echo "The Neo4j test dependency is missing from the active Python environment." >&2
    echo "Install it with: python -m pip install -e \".[dev,mongodb,neo4j,postgres,redis]\"" >&2
    exit 1
fi

if ! "$GENIE_TEST_PYTHON" -c "import pymongo" >/dev/null 2>&1; then
    echo "The MongoDB test dependency is missing from the active Python environment." >&2
    echo "Install it with: python -m pip install -e \".[dev,mongodb,neo4j,postgres,redis]\"" >&2
    exit 1
fi

if ! "$GENIE_TEST_PYTHON" -c "import redis" >/dev/null 2>&1; then
    echo "The Redis test dependency is missing from the active Python environment." >&2
    echo "Install it with: python -m pip install -e \".[dev,mongodb,neo4j,postgres,redis]\"" >&2
    exit 1
fi

require_free_container "$GENIE_TEST_POSTGRES_CONTAINER"
require_free_container "$GENIE_TEST_NEO4J_CONTAINER"
require_free_container "$GENIE_TEST_MONGODB_CONTAINER"
require_free_container "$GENIE_TEST_REDIS_CONTAINER"
require_free_container "$GENIE_TEST_REDIS_REPLICA_CONTAINER"

echo "Compiling Genie..."
"$GENIE_TEST_PYTHON" -m compileall -q lib/src/genie lib/tests lib/benchmarks examples

echo "Starting disposable PostgreSQL container..."
docker run --rm -d \
    --name "$GENIE_TEST_POSTGRES_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_POSTGRES_HOST_PORT}:5432" \
    -e "POSTGRES_USER=${GENIE_TEST_POSTGRES_USER}" \
    -e "POSTGRES_PASSWORD=${GENIE_TEST_POSTGRES_PASSWORD}" \
    -e "POSTGRES_DB=${GENIE_TEST_POSTGRES_DATABASE}" \
    "$GENIE_TEST_POSTGRES_IMAGE" >/dev/null
GENIE_TEST_POSTGRES_STARTED=true

echo "Waiting for PostgreSQL..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_POSTGRES_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    if docker exec "$GENIE_TEST_POSTGRES_CONTAINER" \
        pg_isready -U "$GENIE_TEST_POSTGRES_USER" -d "$GENIE_TEST_POSTGRES_DATABASE" >/dev/null 2>&1; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_POSTGRES_READY_ATTEMPTS)); then
        echo "PostgreSQL did not become ready. Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_POSTGRES_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "Starting disposable Neo4j container..."
docker run --rm -d \
    --name "$GENIE_TEST_NEO4J_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}:7687" \
    -e "NEO4J_AUTH=${GENIE_TEST_NEO4J_USER}/${GENIE_TEST_NEO4J_PASSWORD}" \
    "$GENIE_TEST_NEO4J_IMAGE" >/dev/null
GENIE_TEST_NEO4J_STARTED=true

echo "Starting disposable MongoDB container..."
# The replica set member is configured with its in-container address, so the
# driver must not follow that topology from the host. Connecting with
# `directConnection=true` talks straight to the published port instead.
docker run --rm -d \
    --name "$GENIE_TEST_MONGODB_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_MONGODB_HOST_PORT}:27017" \
    "$GENIE_TEST_MONGODB_IMAGE" \
    mongod --replSet "$GENIE_TEST_MONGODB_REPLICA_SET" --bind_ip_all >/dev/null
GENIE_TEST_MONGODB_STARTED=true

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

echo "Starting disposable Redis replica container..."
# The replica reaches the primary by its address on the Docker bridge network.
# The default bridge gives no name resolution between containers, so read the
# address instead of using the container name.
GENIE_TEST_REDIS_PRIMARY_ADDRESS="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$GENIE_TEST_REDIS_CONTAINER")"
if [[ -z "$GENIE_TEST_REDIS_PRIMARY_ADDRESS" ]]; then
    echo "Could not read the network address of the primary Redis container." >&2
    exit 1
fi
docker run --rm -d \
    --name "$GENIE_TEST_REDIS_REPLICA_CONTAINER" \
    -p "127.0.0.1:${GENIE_TEST_REDIS_REPLICA_HOST_PORT}:6379" \
    "$GENIE_TEST_REDIS_IMAGE" \
    redis-server --replicaof "$GENIE_TEST_REDIS_PRIMARY_ADDRESS" 6379 >/dev/null
GENIE_TEST_REDIS_REPLICA_STARTED=true

echo "Waiting for the Redis replica to follow the primary..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_REDIS_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    # Read the reply into a variable instead of piping it into grep. A pipe
    # that closes early can fail under `set -o pipefail` even on a match.
    GENIE_TEST_REDIS_REPLICA_INFO="$(docker exec "$GENIE_TEST_REDIS_REPLICA_CONTAINER" redis-cli INFO replication 2>/dev/null || true)"
    if [[ "$GENIE_TEST_REDIS_REPLICA_INFO" == *"role:slave"* && "$GENIE_TEST_REDIS_REPLICA_INFO" == *"master_link_status:up"* ]]; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_REDIS_READY_ATTEMPTS)); then
        echo "The Redis replica did not follow the primary at ${GENIE_TEST_REDIS_PRIMARY_ADDRESS}." >&2
        echo "Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_REDIS_REPLICA_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "Waiting for MongoDB..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_MONGODB_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    if docker exec "$GENIE_TEST_MONGODB_CONTAINER" \
        mongosh --quiet --eval "db.adminCommand({ping: 1})" >/dev/null 2>&1; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_MONGODB_READY_ATTEMPTS)); then
        echo "MongoDB did not become ready. Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_MONGODB_CONTAINER" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "Initiating the single-member MongoDB replica set..."
docker exec "$GENIE_TEST_MONGODB_CONTAINER" mongosh --quiet --eval \
    "try { rs.status() } catch (error) { rs.initiate({_id: '${GENIE_TEST_MONGODB_REPLICA_SET}', members: [{_id: 0, host: '127.0.0.1:27017'}]}) }" >/dev/null

echo "Waiting for the MongoDB primary..."
for ((GENIE_TEST_ATTEMPT = 1; GENIE_TEST_ATTEMPT <= GENIE_TEST_MONGODB_READY_ATTEMPTS; GENIE_TEST_ATTEMPT++)); do
    # Read the reply into a variable instead of piping it into grep. A pipe
    # that closes early can fail under `set -o pipefail` even on a match.
    GENIE_TEST_MONGODB_HELLO="$(docker exec "$GENIE_TEST_MONGODB_CONTAINER" mongosh --quiet --eval "db.hello().isWritablePrimary" 2>/dev/null || true)"
    if [[ "$GENIE_TEST_MONGODB_HELLO" == *"true"* ]]; then
        break
    fi

    if ((GENIE_TEST_ATTEMPT == GENIE_TEST_MONGODB_READY_ATTEMPTS)); then
        echo "The MongoDB replica set did not elect a primary." >&2
        echo "Recent container logs:" >&2
        docker logs --tail 100 "$GENIE_TEST_MONGODB_CONTAINER" >&2 || true
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

echo "Running $GENIE_TEST_LABEL with live PostgreSQL, Neo4j, MongoDB, and Redis enabled..."
env \
    GENIE_POSTGRES_DSN="postgresql://${GENIE_TEST_POSTGRES_USER}:${GENIE_TEST_POSTGRES_PASSWORD}@127.0.0.1:${GENIE_TEST_POSTGRES_HOST_PORT}/${GENIE_TEST_POSTGRES_DATABASE}" \
    GENIE_POSTGRES_SCHEMA="$GENIE_TEST_POSTGRES_SCHEMA" \
    GENIE_NEO4J_URI="bolt://127.0.0.1:${GENIE_TEST_NEO4J_HOST_PORT}" \
    GENIE_NEO4J_USER="$GENIE_TEST_NEO4J_USER" \
    GENIE_NEO4J_PASSWORD="$GENIE_TEST_NEO4J_PASSWORD" \
    GENIE_NEO4J_DATABASE="$GENIE_TEST_NEO4J_DATABASE" \
    GENIE_MONGODB_URI="mongodb://127.0.0.1:${GENIE_TEST_MONGODB_HOST_PORT}/?directConnection=true" \
    GENIE_MONGODB_DATABASE="$GENIE_TEST_MONGODB_DATABASE" \
    GENIE_REDIS_URL="redis://127.0.0.1:${GENIE_TEST_REDIS_HOST_PORT}/${GENIE_TEST_REDIS_DATABASE}" \
    GENIE_REDIS_REPLICA_URL="redis://127.0.0.1:${GENIE_TEST_REDIS_REPLICA_HOST_PORT}/${GENIE_TEST_REDIS_DATABASE}" \
    GENIE_REDIS_KEY_PREFIX="$GENIE_TEST_REDIS_KEY_PREFIX" \
    $GENIE_TEST_COMMAND "$@"
