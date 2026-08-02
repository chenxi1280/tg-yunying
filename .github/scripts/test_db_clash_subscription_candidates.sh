#!/usr/bin/env bash
set -euo pipefail

SUBSCRIPTION_ID="${CLASH_SUBSCRIPTION_ID:?CLASH_SUBSCRIPTION_ID is required}"
NODE_LIMIT="${CLASH_NODE_LIMIT:-16}"
CLASH_IMAGE="${CLASH_IMAGE:-metacubex/mihomo:latest}"
SCRIPT_PATH="${CLASH_SETUP_SCRIPT:-/tmp/tgyunying_configure_clash_search_join_live.py}"
PREFIX="tgyunying-mihomo-dbnext-${SUBSCRIPTION_ID}"
CANDIDATE_DIR="/tmp/tgyunying-db-candidates-${SUBSCRIPTION_ID}"
LIVE_APPLY="${CLASH_LIVE_APPLY:-false}"
SUBSCRIPTION_NAME="${CLASH_SUBSCRIPTION_NAME:-主订阅}"
SUBSCRIPTION_PRIORITY="${CLASH_SUBSCRIPTION_PRIORITY:-10}"
HEALTHY_INDEXES=""

cleanup() {
  docker ps -a --format '{{.Names}}' | grep "^${PREFIX}-" | xargs -r docker rm -f >/dev/null || true
  rm -rf "${CANDIDATE_DIR}"
}

prepare_configs() {
  docker exec tgyunying-backend sh -lc 'rm -rf /tmp/tgyunying-mihomo-configs'
  docker exec \
    -e CLASH_SUBSCRIPTION_ID="${SUBSCRIPTION_ID}" \
    -e CLASH_NODE_LIMIT="${NODE_LIMIT}" \
    -e CLASH_SETUP_PHASE=prepare_configs \
    -i tgyunying-backend python - < "${SCRIPT_PATH}"
  mkdir -p "${CANDIDATE_DIR}"
  docker cp tgyunying-backend:/tmp/tgyunying-mihomo-configs/. "${CANDIDATE_DIR}/"
}

start_candidates() {
  local config suffix name
  docker pull "${CLASH_IMAGE}" >/dev/null
  for config in "${CANDIDATE_DIR}"/tgyunying-mihomo-*.yaml; do
    suffix="$(basename "${config}" .yaml)"
    suffix="${suffix##*-}"
    name="${PREFIX}-${suffix}"
    docker run -d --restart no --name "${name}" --network infra_default \
      -v "${config}:/root/.config/mihomo/config.yaml:ro" "${CLASH_IMAGE}" >/dev/null
  done
}

probe_candidates() {
  local config suffix name healthy=0 failed=0
  sleep 5
  for config in "${CANDIDATE_DIR}"/tgyunying-mihomo-*.yaml; do
    suffix="$(basename "${config}" .yaml)"
    suffix="${suffix##*-}"
    name="${PREFIX}-${suffix}"
    if timeout 20 docker exec tgyunying-backend curl -fsS --max-time 12 \
      -x "socks5h://${name}:7890" https://api.ipify.org >/dev/null; then
      echo "CLASH_DB_CANDIDATE_EGRESS=${name}:ok"
      HEALTHY_INDEXES="${HEALTHY_INDEXES:+${HEALTHY_INDEXES},}$((10#${suffix}))"
      healthy=$((healthy + 1))
    else
      echo "CLASH_DB_CANDIDATE_EGRESS=${name}:failed"
      failed=$((failed + 1))
    fi
  done
  echo "CLASH_DB_CANDIDATE_SUMMARY=subscription_id=${SUBSCRIPTION_ID}:healthy=${healthy}:failed=${failed}"
}

active_account_count() {
  docker exec tgyunying-backend python -c \
    'from sqlalchemy import func, select; from app.database import SessionLocal; from app.models import AccountStatus, TgAccount; s=SessionLocal(); print(int(s.scalar(select(func.count(TgAccount.id)).where(TgAccount.tenant_id == 1, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value, TgAccount.session_ciphertext.is_not(None), TgAccount.account_identity != "code_receiver")) or 0)); s.close()'
}

run_db_phase() {
  local phase="$1" account_count="$2"
  docker exec \
    -e CLASH_SUBSCRIPTION_ID="${SUBSCRIPTION_ID}" \
    -e CLASH_SUBSCRIPTION_NAME="${SUBSCRIPTION_NAME}" \
    -e CLASH_SUBSCRIPTION_PRIORITY="${SUBSCRIPTION_PRIORITY}" \
    -e CLASH_NODE_LIMIT="${NODE_LIMIT}" \
    -e CLASH_HEALTHY_INDEXES="${HEALTHY_INDEXES}" \
    -e CLASH_TEST_ACCOUNT_COUNT="${account_count}" \
    -e CLASH_CREATE_SMOKE_TASK=false \
    -e CLASH_LIVE_APPLY=true \
    -e CLASH_SETUP_PHASE="${phase}" \
    -i tgyunying-backend python - < "${SCRIPT_PATH}"
}

apply_live() {
  local account_count suffix index
  test -n "${HEALTHY_INDEXES}"
  account_count="$(active_account_count)"
  test "${account_count}" -gt 0
  run_db_phase preflight_db "${account_count}"
  docker ps -a --format '{{.Names}}' | grep '^tgyunying-mihomo-[0-9][0-9][0-9]$' | xargs -r docker rm -f >/dev/null
  for index in ${HEALTHY_INDEXES//,/ }; do
    suffix="$(printf '%03d' "${index}")"
    docker rename "${PREFIX}-${suffix}" "tgyunying-mihomo-${suffix}"
  done
  run_db_phase apply_db "${account_count}"
}

trap cleanup EXIT
cleanup
prepare_configs
start_candidates
probe_candidates
if [ "${LIVE_APPLY}" = "true" ]; then
  apply_live
fi
