#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-/data/tgyunying}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SLOT_ID="${SLOT_ID:-slot-01}"
SLOT_NUMBER="${SLOT_NUMBER:-01}"
PORT="${PORT:-18101}"
SERVICE_USER="${SERVICE_USER:-tgy-agy-${SLOT_NUMBER}}"
SOURCE_AGY_BIN="${SOURCE_AGY_BIN:-/root/.local/bin/agy}"
SHARED_DIR="${BASE_DIR}/shared/antigravity/${SLOT_ID}"
ENV_FILE="${SHARED_DIR}/provider.env"
SERVICE_NAME="tgyunying-antigravity-${SLOT_ID}.service"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"
RUNTIME_ROOT="${ANTIGRAVITY_RUNTIME_ROOT:-/usr/local/lib/tgyunying-antigravity}"
LOCK_FILE="${ANTIGRAVITY_SLOT_LOCK_FILE:-/run/lock/tgyunying-antigravity-provider.lock}"
STATE_CHECKER="${ANTIGRAVITY_SLOT_STATE_CHECKER:-${SCRIPT_DIR}/check-antigravity-slot-install-state.sh}"
PYTHON_BIN="${ANTIGRAVITY_PYTHON_BIN:-/usr/bin/python3.11}"
INFRA_NETWORK_NAME="${INFRA_NETWORK_NAME:-infra_default}"

require_unit_inactive() {
  bash "${STATE_CHECKER}" "${SERVICE_NAME}"
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi
if [[ ! -x "${SOURCE_AGY_BIN}" ]]; then
  echo "Antigravity CLI missing: ${SOURCE_AGY_BIN}" >&2
  exit 1
fi
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ANTIGRAVITY_PYTHON_MISSING=${PYTHON_BIN}" >&2
  exit 1
fi
"${PYTHON_BIN}" -E -s -c 'import cryptography, sqlite3' || {
  echo "ANTIGRAVITY_PYTHON_DEPENDENCY_MISSING=${PYTHON_BIN}" >&2
  exit 1
}
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "ANTIGRAVITY_SLOT_OPERATION_LOCKED=${LOCK_FILE}" >&2
  exit 1
fi
require_unit_inactive
if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
chmod 0700 "${SERVICE_HOME}"
SERVICE_AUTH_DIR="${SERVICE_HOME}/.gemini"
SERVICE_CONFIG_DIR="${SERVICE_HOME}/.config/antigravity"
SERVICE_CACHE_DIR="${SERVICE_HOME}/.cache/antigravity"
SERVICE_DATA_DIR="${SERVICE_HOME}/.local/share/antigravity"
SERVICE_LEDGER_DIR="${SERVICE_DATA_DIR}/ledger"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 \
  "${SERVICE_AUTH_DIR}" "${SERVICE_CONFIG_DIR}" "${SERVICE_CACHE_DIR}" \
  "${SERVICE_DATA_DIR}" "${SERVICE_LEDGER_DIR}"
install -o root -g root -m 0755 "${SOURCE_AGY_BIN}" /usr/local/bin/agy
install -d -o root -g root -m 0700 "${SHARED_DIR}"
release_sha="$(sed -n 's/^RELEASE_SHA=//p' "${PROJECT_DIR}/.image.env")"
SOURCE_DIR="${PROJECT_DIR}/backend/scripts" \
RELEASE_SHA="${release_sha}" \
ANTIGRAVITY_RUNTIME_ROOT="${RUNTIME_ROOT}" \
ANTIGRAVITY_RUNTIME_PYTHON_BIN="${PYTHON_BIN}" \
  bash "${SCRIPT_DIR}/install-antigravity-provider-runtime.sh"

configured_ledger="-"
if [[ -f "${ENV_FILE}" ]]; then
  bridge_token="$(sed -n 's/^ANTIGRAVITY_BRIDGE_TOKEN=//p' "${ENV_FILE}")"
  ledger_key="$(sed -n 's/^ANTIGRAVITY_LEDGER_KEY=//p' "${ENV_FILE}")"
  configured_ledger="$(sed -n 's/^ANTIGRAVITY_LEDGER_PATH=//p' "${ENV_FILE}")"
  [[ -n "${bridge_token}" && -n "${ledger_key}" ]]
  [[ -n "${configured_ledger}" ]] || configured_ledger="-"
else
  bridge_token="$(openssl rand -hex 32)"
  ledger_key="$("${PYTHON_BIN}" -E -s - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode("ascii"))
PY
)"
fi
legacy_ledger="${SHARED_DIR}/requests.sqlite3"
service_ledger="${SERVICE_LEDGER_DIR}/requests.sqlite3"
require_unit_inactive
ANTIGRAVITY_PROVIDER_SCRIPTS_DIR="${PROJECT_DIR}/backend/scripts" \
  "${PYTHON_BIN}" -E -s "${SCRIPT_DIR}/migrate-antigravity-provider-ledger.py" \
  "${configured_ledger}" "${legacy_ledger}" "${service_ledger}" "${SERVICE_USER}"
require_unit_inactive
env_tmp="$(mktemp "${SHARED_DIR}/provider.env.XXXXXX")"
cat >"${env_tmp}" <<EOF
HOME=${SERVICE_HOME}
ANTIGRAVITY_SLOT_ID=${SLOT_ID}
ANTIGRAVITY_BRIDGE_TOKEN=${bridge_token}
ANTIGRAVITY_LEDGER_KEY=${ledger_key}
ANTIGRAVITY_LEDGER_PATH=${service_ledger}
ANTIGRAVITY_CLI_BIN=/usr/local/bin/agy
ANTIGRAVITY_MAX_TIMEOUT_SECONDS=180
EOF
chown root:root "${env_tmp}"
chmod 0600 "${env_tmp}"
mv -f "${env_tmp}" "${ENV_FILE}"

docker_gateway="$(docker network inspect "${INFRA_NETWORK_NAME}" --format '{{(index .IPAM.Config 0).Gateway}}')"
if [[ -z "${docker_gateway}" ]]; then
  echo "${INFRA_NETWORK_NAME} gateway missing" >&2
  exit 1
fi

cat >"${UNIT_FILE}" <<EOF
[Unit]
Description=TG Yunying Antigravity Provider ${SLOT_ID}
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${RUNTIME_ROOT}/current
ExecStart=${PYTHON_BIN} -E -s ${RUNTIME_ROOT}/current/antigravity_provider_server.py --host ${docker_gateway} --port ${PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
UMask=0077
InaccessiblePaths=-${BASE_DIR}/shared/.env -${BASE_DIR}/shared/authorization-dr-snapshots
ReadWritePaths=${SERVICE_AUTH_DIR} ${SERVICE_CONFIG_DIR} ${SERVICE_CACHE_DIR} ${SERVICE_DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "SLOT_ID=${SLOT_ID} SERVICE_USER=${SERVICE_USER} HOST=${docker_gateway} PORT=${PORT}"
echo "Run OAuth as: sudo -u ${SERVICE_USER} -H /usr/local/bin/agy"
