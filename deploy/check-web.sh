#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/docker-env.sh"

ensure_runtime_env

ATTEMPTS="${TGYUNYING_CHECK_ATTEMPTS:-12}"
RETRY_DELAY="${TGYUNYING_CHECK_RETRY_DELAY_SECONDS:-5}"
WORKER_READY_TIMEOUT="${TGYUNYING_WORKER_READY_TIMEOUT_SECONDS:-360}"
PLANNER_SMOKE_MODE="${TGYUNYING_PLANNER_SMOKE_MODE:-healthcheck}"
BACKEND_URL="http://${TGYUNYING_BACKEND_BIND_HOST:-127.0.0.1}:${TGYUNYING_BACKEND_HOST_PORT:-18090}"
STATIC_DIR="${TGYUNYING_FRONTEND_STATIC_BASE_DIR:-/data/infra/www/${TGYUNYING_WEB_HOST:-tgyunying}}/current"
JS_ASSET_PATTERN='src="/assets/[^"]+\.js"'
GZIP_HEADER_PATTERN='^content-encoding:[[:space:]]*gzip[[:space:]]*$'

require_command curl
require_command docker

check_url() {
  local label="$1"
  local url="$2"
  local resolve_arg="${3:-}"
  local attempt status body

  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if [[ -n "$resolve_arg" ]]; then
      status="$(curl -k -L -sS -o /tmp/tgyunying-check-body.txt -w '%{http_code}' --max-time 10 --resolve "$resolve_arg" "$url" || true)"
    else
      status="$(curl -k -L -sS -o /tmp/tgyunying-check-body.txt -w '%{http_code}' --max-time 10 "$url" || true)"
    fi
    body="$(cat /tmp/tgyunying-check-body.txt 2>/dev/null || true)"
    if [[ "$status" =~ ^[23] ]]; then
      echo "OK ${label}: ${url} -> HTTP ${status}"
      return 0
    fi
    echo "Waiting for ${label}: ${url} -> HTTP ${status:-curl-failed} (${attempt}/${ATTEMPTS})"
    if (( attempt < ATTEMPTS )); then
      sleep "$RETRY_DELAY"
    fi
  done

  echo "BAD ${label}: ${url} -> HTTP ${status:-curl-failed}" >&2
  echo "$body" | tail -c 1000 >&2
  return 1
}

check_frontend_gzip() {
  local origin="$1"
  local resolve_arg="${2:-}"
  local index_html js_path headers

  if [[ -n "$resolve_arg" ]]; then
    index_html="$(curl -k -L -sS --max-time 10 --resolve "$resolve_arg" "${origin}/" || true)"
  else
    index_html="$(curl -k -L -sS --max-time 10 "${origin}/" || true)"
  fi

  js_path="$(printf '%s' "$index_html" | grep -oE "$JS_ASSET_PATTERN" | head -1 | cut -d '"' -f 2)"
  if [[ -z "$js_path" ]]; then
    echo "BAD frontend gzip: no JS asset found in ${origin}/" >&2
    return 1
  fi

  if [[ -n "$resolve_arg" ]]; then
    headers="$(curl -k -L -sS -D - -o /dev/null --max-time 10 --resolve "$resolve_arg" -H 'Accept-Encoding: gzip' "${origin}${js_path}" || true)"
  else
    headers="$(curl -k -L -sS -D - -o /dev/null --max-time 10 -H 'Accept-Encoding: gzip' "${origin}${js_path}" || true)"
  fi

  if printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' | grep -Eq "$GZIP_HEADER_PATTERN"; then
    echo "OK frontend gzip: ${origin}${js_path}"
    return 0
  fi

  echo "BAD frontend gzip: ${origin}${js_path} did not return Content-Encoding: gzip" >&2
  printf '%s\n' "$headers" | tail -30 >&2
  return 1
}

check_frontend_release() {
  local origin="$1"
  local resolve_arg="$2"
  local expected_path actual_html actual_path
  expected_path="$(grep -oE "$JS_ASSET_PATTERN" "${STATIC_DIR}/index.html" | head -1 | cut -d '"' -f 2)"
  actual_html="$(curl -k -L -sS --max-time 10 --resolve "$resolve_arg" "${origin}/" || true)"
  actual_path="$(printf '%s' "$actual_html" | grep -oE "$JS_ASSET_PATTERN" | head -1 | cut -d '"' -f 2)"
  if [[ -n "$expected_path" && "$actual_path" == "$expected_path" ]]; then
    echo "OK frontend release: ${origin}${actual_path}"
    return 0
  fi
  echo "BAD frontend release mismatch: expected=${expected_path:-missing} actual=${actual_path:-missing}" >&2
  return 1
}

wait_for_worker_ready() {
  local container_name="$1"
  local started_at status health elapsed
  started_at="$(date +%s)"

  while true; do
    status="$(docker inspect "$container_name" --format '{{.State.Status}}' 2>/dev/null || true)"
    health="$(docker inspect "$container_name" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
    if [[ "$status" == "running" && ( -z "$health" || "$health" == "healthy" ) ]]; then
      echo "OK worker container ${container_name}: status=$status health=${health:-none}"
      return 0
    fi
    if [[ "$status" == "exited" || "$status" == "dead" ]]; then
      echo "BAD worker container ${container_name}: status=${status:-missing} health=${health:-none}" >&2
      docker logs --tail 200 "$container_name" >&2 || true
      return 1
    fi
    elapsed=$(($(date +%s) - started_at))
    if (( elapsed >= WORKER_READY_TIMEOUT )); then
      echo "BAD worker container ${container_name}: timed out waiting for health, status=${status:-missing} health=${health:-none}" >&2
      docker logs --tail 200 "$container_name" >&2 || true
      return 1
    fi
    sleep 5
  done
}

run_planner_smoke_check() {
  local limit="${TGYUNYING_PLANNER_SMOKE_LIMIT:-1}"
  local timeout_seconds="${TGYUNYING_PLANNER_SMOKE_TIMEOUT_SECONDS:-120}"
  echo "==> Running planner smoke check (${PLANNER_SMOKE_MODE})"
  if [[ "$PLANNER_SMOKE_MODE" == "healthcheck" ]]; then
    timeout "$timeout_seconds" docker exec tgyunying-worker-planner python -m app.worker_health --role planner
    return
  fi
  if [[ "$PLANNER_SMOKE_MODE" == "once" ]]; then
    timeout "$timeout_seconds" docker exec tgyunying-worker-planner python -m app.worker --once --role planner --limit "$limit"
    return
  fi
  echo "BAD planner smoke mode: ${PLANNER_SMOKE_MODE}" >&2
  return 1
}

check_image_verification_readiness() {
  local timeout_seconds="${IMAGE_VERIFICATION_WORKER_READY_TIMEOUT_SECONDS:-180}"
  echo "==> Checking image verification engines"
  timeout "$timeout_seconds" docker exec tgyunying-image-verification-worker \
    python -c "import json, os, urllib.request; request = urllib.request.Request('http://127.0.0.1:8091/internal/v1/image-verification/ready', headers={'X-Internal-Token': os.environ['IMAGE_VERIFICATION_WORKER_TOKEN']}); payload = json.load(urllib.request.urlopen(request, timeout=120)); assert payload.get('status') == 'ready'; assert set(payload.get('engines') or ()) == {'rapidocr', 'ddddocr'}"
  timeout "$timeout_seconds" docker exec -i \
    tgyunying-image-verification-worker python - <<'PY'
import base64
import hashlib
import io
import json
import os
import secrets
import urllib.request
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageDraw

image = Image.new("RGB", (180, 60), "white")
ImageDraw.Draw(image).text((20, 18), "12+7", fill="black")
buffer = io.BytesIO()
image.save(buffer, format="PNG")
image_bytes = buffer.getvalue()
payload = {
    "request_id": secrets.token_hex(32),
    "action_id": "release-functional-probe",
    "challenge_fingerprint_hash": hashlib.sha256(b"release-probe").hexdigest(),
    "image_base64": base64.b64encode(image_bytes).decode("ascii"),
    "mime_type": "image/png",
    "verification_kind": "alphanumeric",
    "candidate_hash": hashlib.sha256(image_bytes).hexdigest(),
    "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
    "remaining_budget_ms": 120_000,
    "contract_version": os.environ["IMAGE_VERIFICATION_CONTRACT_VERSION"],
}
request = urllib.request.Request(
    "http://127.0.0.1:8091/internal/v1/image-verification/ocr",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "X-Internal-Token": os.environ["IMAGE_VERIFICATION_WORKER_TOKEN"],
    },
)
response = json.load(urllib.request.urlopen(request, timeout=120))
sources = response.get("sources") or ()
assert response.get("status") == "completed"
assert {source.get("source") for source in sources} == {"rapidocr", "ddddocr"}
assert all(source.get("status") == "complete" for source in sources)
PY
  echo "OK image verification engines and inference: rapidocr, ddddocr"
}

backend_status="$(docker inspect tgyunying-backend --format '{{.State.Status}}' 2>/dev/null || true)"
backend_health="$(docker inspect tgyunying-backend --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
worker_containers=(
  tgyunying-worker-planner
  tgyunying-worker-ai-generation
  tgyunying-worker-ai-generation-2
  tgyunying-worker-ai-generation-3
  tgyunying-worker-dispatcher-1
  tgyunying-worker-dispatcher-2
  tgyunying-worker-search-dispatcher
  tgyunying-worker-listener
  tgyunying-worker-recovery
  tgyunying-worker-account-security
  tgyunying-worker-material-cache
  tgyunying-worker-voice-profile
  tgyunying-worker-account-online
  tgyunying-worker-ai-memory
  tgyunying-worker-metrics
)

if verification_remote_enabled; then
  worker_containers=(
    tgyunying-image-verification-worker
    "${worker_containers[@]}"
  )
fi

if [[ "$backend_status" != "running" || ( -n "$backend_health" && "$backend_health" != "healthy" ) ]]; then
  echo "BAD backend container: status=${backend_status:-missing} health=${backend_health:-none}" >&2
  docker logs --tail 200 tgyunying-backend >&2 || true
  exit 1
fi
echo "OK backend container: status=$backend_status health=${backend_health:-none}"

for worker_container in "${worker_containers[@]}"; do
  wait_for_worker_ready "$worker_container"
done
if verification_remote_enabled; then
  check_image_verification_readiness
fi
run_planner_smoke_check

if [[ ! -f "${STATIC_DIR}/index.html" ]]; then
  echo "BAD frontend static index missing: ${STATIC_DIR}/index.html" >&2
  exit 1
fi
echo "OK frontend static index: ${STATIC_DIR}/index.html"

check_url "local api health" "${BACKEND_URL}/api/health"

case "${TGYUNYING_CHECK_HOST_NGINX:-true}" in
  false|False|FALSE|0|no|No|NO|off|Off|OFF)
    echo "Host Nginx checks skipped"
    ;;
  *)
    if [[ -z "${TGYUNYING_WEB_HOST:-}" ]]; then
      echo "TGYUNYING_WEB_HOST is empty; skip host Nginx checks"
    else
      check_url "host nginx api health" "https://${TGYUNYING_WEB_HOST}/api/health" "${TGYUNYING_WEB_HOST}:443:127.0.0.1"
      check_frontend_release "https://${TGYUNYING_WEB_HOST}" "${TGYUNYING_WEB_HOST}:443:127.0.0.1"
      check_frontend_gzip "https://${TGYUNYING_WEB_HOST}" "${TGYUNYING_WEB_HOST}:443:127.0.0.1"
    fi
    ;;
esac

case "${TGYUNYING_CHECK_PUBLIC_URLS:-true}" in
  false|False|FALSE|0|no|No|NO|off|Off|OFF)
    echo "Public URL checks skipped"
    ;;
  *)
    if [[ -z "${TGYUNYING_WEB_HOST:-}" ]]; then
      echo "TGYUNYING_WEB_HOST is empty; skip public URL checks"
    else
      check_url "public frontend" "https://${TGYUNYING_WEB_HOST}/"
      check_url "public api health" "https://${TGYUNYING_WEB_HOST}/api/health"
    fi
    ;;
esac

echo "Post-deploy checks passed"
