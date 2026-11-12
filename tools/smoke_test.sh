#!/usr/bin/env bash
# End-to-end smoke test: simulator -> collector -> PostgreSQL via docker compose.
# Usage: cp .env.example .env && tools/smoke_test.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Compose reads .env itself; load it here too for the psql credentials.
set -a
# shellcheck disable=SC1091
source .env
set +a

API_PORT="${COLLECTOR_API_PORT:-8080}"
API="http://127.0.0.1:${API_PORT}"
status=1

cleanup() {
  if [ "$status" -ne 0 ]; then
    echo "SMOKE TEST FAILED, compose logs follow:" >&2
    docker compose logs >&2 || true
  fi
  docker compose down -v
}
trap 'status=$?; cleanup; exit $status' EXIT

count_readings() {
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -tAc "select count(*) from tag_readings where quality = 'good'"
}

echo "== Building and starting the stack"
docker compose up -d --build

echo "== Waiting for /health/ready (max 90 s)"
ready=0
for _ in $(seq 1 45); do
  if [ "$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$API/health/ready" || true)" = "200" ]; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" -eq 1 ] || { echo "collector never became ready" >&2; exit 1; }

echo "== Checking worker state"
workers="$(curl -sf --max-time 5 "$API/api/runtime/workers")"
echo "$workers"
# Every worker must be running (parsed as JSON, not grepped).
echo "$workers" | python3 -c '
import json, sys
workers = json.load(sys.stdin)["workers"]
bad = [w["component_id"] for w in workers if w["state"] != "running"]
sys.exit(0 if workers and not bad else 1)
' || { echo "not all workers are running" >&2; exit 1; }

echo "== Checking that good-quality readings grow (max 20 s)"
first="$(count_readings)"
grew=0
for _ in $(seq 1 10); do
  sleep 2
  second="$(count_readings)"
  if [ "$second" -gt "$first" ]; then
    grew=1
    break
  fi
done
echo "good tag_readings: $first -> $second"
[ "$grew" -eq 1 ] || { echo "good readings did not grow" >&2; exit 1; }

echo "SMOKE TEST PASSED"
status=0
