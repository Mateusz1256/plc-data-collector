#!/usr/bin/env bash
# End-to-end smoke test: simulator -> collector -> PostgreSQL via docker compose,
# then the Panel sign-in, an API key created in the Panel, and the /api/v1 data API.
# Usage: cp .env.example .env && tools/smoke_test.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Compose reads .env itself; load it here too for the psql credentials.
set -a
# shellcheck disable=SC1091
source .env
set +a
if [ -z "${PLC_DATA_COLLECTOR_ADMIN_PASSWORD:-}" ]; then
  echo "PLC_DATA_COLLECTOR_ADMIN_PASSWORD is empty in .env; the Panel checks need it" >&2
  exit 1
fi

API_PORT="${COLLECTOR_API_PORT:-8080}"
API="http://127.0.0.1:${API_PORT}"
status=1
# Panel session cookie; removed on exit.
cookie_jar="$(mktemp)"

cleanup() {
  rm -f "$cookie_jar"
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

echo "== Checking the Panel sign-in page"
login_code="$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$API/panel/login" || true)"
[ "$login_code" = "200" ] || { echo "GET /panel/login returned $login_code" >&2; exit 1; }

echo "== Signing in to the Panel"
# The password goes through stdin, so it is not on the curl command line.
# A correct password answers 303 and sets the session cookie; a wrong one answers 401.
signin_code="$(printf '%s' "$PLC_DATA_COLLECTOR_ADMIN_PASSWORD" \
  | curl -s --max-time 10 -c "$cookie_jar" -o /dev/null -w '%{http_code}' \
      --data-urlencode 'password@-' --data-urlencode 'next=/panel/live' \
      "$API/panel/login" || true)"
[ "$signin_code" = "303" ] || { echo "sign-in returned $signin_code, expected 303" >&2; exit 1; }

live_page="$(curl -sf --max-time 10 -b "$cookie_jar" "$API/panel/live")"
grep -q 'Line 1 temperature' <<<"$live_page" \
  || { echo "/panel/live does not list Line 1 temperature" >&2; exit 1; }

echo "== Creating an API key in the Panel"
settings_page="$(curl -sf --max-time 10 -b "$cookie_jar" \
  --data-urlencode 'name=Smoke test' "$API/panel/settings/api-keys")"
api_key="$(grep -o 'pdc_[0-9a-f]\{48\}' <<<"$settings_page" | head -n 1 || true)"
[ -n "$api_key" ] || { echo "no API key in the Settings page" >&2; exit 1; }

echo "== Checking current values in /api/v1/tags"
curl -sf --max-time 10 -H "X-API-Key: $api_key" "$API/api/v1/tags" | python3 -c '
import json, sys
tags = json.load(sys.stdin)["tags"]
with_value = sum(tag["current"] is not None for tag in tags)
print("tags:", len(tags), "with a current value:", with_value)
sys.exit(0 if with_value else 1)
' || { echo "no Tag has a current value" >&2; exit 1; }

echo "== Checking stored history in /api/v1/tags/line1_temperature/history"
curl -sf --max-time 10 -H "X-API-Key: $api_key" \
  "$API/api/v1/tags/line1_temperature/history" | python3 -c '
import json, sys
readings = json.load(sys.stdin)["readings"]
print("line1_temperature readings in the last hour:", len(readings))
sys.exit(0 if readings else 1)
' || { echo "line1_temperature has no stored readings" >&2; exit 1; }

echo "SMOKE TEST PASSED"
status=0
