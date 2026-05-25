#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/docker-env.sh"

ensure_runtime_env

ATTEMPTS="${TGYUNYING_CHECK_ATTEMPTS:-12}"
RETRY_DELAY="${TGYUNYING_CHECK_RETRY_DELAY_SECONDS:-5}"
BACKEND_URL="http://${TGYUNYING_BACKEND_BIND_HOST:-127.0.0.1}:${TGYUNYING_BACKEND_HOST_PORT:-18090}"
STATIC_DIR="${TGYUNYING_FRONTEND_STATIC_BASE_DIR:-/data/infra/www/${TGYUNYING_WEB_HOST:-tgyunying}}/current"

require_command curl

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

backend_status="$(docker inspect tgyunying-backend --format '{{.State.Status}}' 2>/dev/null || true)"
backend_health="$(docker inspect tgyunying-backend --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
worker_containers=(
  tgyunying-worker-planner
  tgyunying-worker-dispatcher-1
  tgyunying-worker-dispatcher-2
  tgyunying-worker-listener
  tgyunying-worker-recovery
  tgyunying-worker-account-security
  tgyunying-worker-metrics
)

if [[ "$backend_status" != "running" || ( -n "$backend_health" && "$backend_health" != "healthy" ) ]]; then
  echo "BAD backend container: status=${backend_status:-missing} health=${backend_health:-none}" >&2
  docker logs --tail 200 tgyunying-backend >&2 || true
  exit 1
fi
echo "OK backend container: status=$backend_status health=${backend_health:-none}"

for worker_container in "${worker_containers[@]}"; do
  worker_status="$(docker inspect "$worker_container" --format '{{.State.Status}}' 2>/dev/null || true)"
  if [[ "$worker_status" != "running" ]]; then
    echo "BAD worker container ${worker_container}: status=${worker_status:-missing}" >&2
    docker logs --tail 200 "$worker_container" >&2 || true
    exit 1
  fi
  echo "OK worker container ${worker_container}: status=$worker_status"
done

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
