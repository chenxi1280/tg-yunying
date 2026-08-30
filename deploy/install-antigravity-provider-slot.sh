#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-/data/tgyunying}"
SLOT_ID="${SLOT_ID:-slot-01}"
SLOT_NUMBER="${SLOT_NUMBER:-01}"
PORT="${PORT:-18101}"
SERVICE_USER="${SERVICE_USER:-tgy-agy-${SLOT_NUMBER}}"
SOURCE_AGY_BIN="${SOURCE_AGY_BIN:-/root/.local/bin/agy}"
SHARED_DIR="${BASE_DIR}/shared/antigravity/${SLOT_ID}"
ENV_FILE="${SHARED_DIR}/provider.env"
SERVICE_NAME="tgyunying-antigravity-${SLOT_ID}.service"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi
if [[ ! -x "${SOURCE_AGY_BIN}" ]]; then
  echo "Antigravity CLI missing: ${SOURCE_AGY_BIN}" >&2
  exit 1
fi
if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi
install -o root -g root -m 0755 "${SOURCE_AGY_BIN}" /usr/local/bin/agy
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${SHARED_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  bridge_token="$(openssl rand -hex 32)"
  ledger_key="$(python3 - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode("ascii"))
PY
)"
  cat >"${ENV_FILE}" <<EOF
HOME=$(getent passwd "${SERVICE_USER}" | cut -d: -f6)
ANTIGRAVITY_SLOT_ID=${SLOT_ID}
ANTIGRAVITY_BRIDGE_TOKEN=${bridge_token}
ANTIGRAVITY_LEDGER_KEY=${ledger_key}
ANTIGRAVITY_LEDGER_PATH=${SHARED_DIR}/requests.sqlite3
ANTIGRAVITY_CLI_BIN=/usr/local/bin/agy
ANTIGRAVITY_MAX_TIMEOUT_SECONDS=180
EOF
  chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
  chmod 0600 "${ENV_FILE}"
fi

docker_gateway="$(docker network inspect infra_default --format '{{(index .IPAM.Config 0).Gateway}}')"
if [[ -z "${docker_gateway}" ]]; then
  echo "infra_default gateway missing" >&2
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
WorkingDirectory=${BASE_DIR}/current/backend/scripts
ExecStart=/usr/bin/python3 ${BASE_DIR}/current/backend/scripts/antigravity_provider_server.py --host ${docker_gateway} --port ${PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${SHARED_DIR}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "SLOT_ID=${SLOT_ID} SERVICE_USER=${SERVICE_USER} HOST=${docker_gateway} PORT=${PORT}"
echo "Run OAuth as: sudo -u ${SERVICE_USER} -H /usr/local/bin/agy"
