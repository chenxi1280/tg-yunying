#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:?BASE_DIR is required}"
RELEASE_DIR="${RELEASE_DIR:?RELEASE_DIR is required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN_SCRIPT="${ANTIGRAVITY_SLOT_PLAN_SCRIPT:-${SCRIPT_DIR}/antigravity-slot-release-plan.sh}"
RUNTIME_INSTALLER="${ANTIGRAVITY_RUNTIME_INSTALLER:-${SCRIPT_DIR}/install-antigravity-provider-runtime.sh}"
PROBE_SCRIPT="${ANTIGRAVITY_PROBE_SCRIPT:-${RELEASE_DIR}/deploy/check-antigravity-provider-slot.py}"
RUNTIME_ROOT="${ANTIGRAVITY_RUNTIME_ROOT:-/usr/local/lib/tgyunying-antigravity}"
INFRA_NETWORK_NAME="${INFRA_NETWORK_NAME:-infra_default}"
PYTHON_BIN="${ANTIGRAVITY_PYTHON_BIN:-/usr/bin/python3.11}"
RUNTIME_PYTHON_BIN="${ANTIGRAVITY_RUNTIME_PYTHON_BIN:-/usr/bin/python3.11}"
TIMEOUT_BIN="${ANTIGRAVITY_TIMEOUT_BIN:-timeout}"
LOCK_FILE="${ANTIGRAVITY_SLOT_LOCK_FILE:-/run/lock/tgyunying-antigravity-provider.lock}"
source "${SCRIPT_DIR}/antigravity-runtime-files.sh"

_restore_runtime() {
  local previous_target="$1"
  local current="${RUNTIME_ROOT}/current"
  if [[ -n "${previous_target}" ]]; then
    ln -sfn "${previous_target}" "${current}.rollback"
    "${RUNTIME_PYTHON_BIN}" -E -s -c \
      'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
      "${current}.rollback" "${current}"
    return
  fi
  [[ ! -e "${current}" || -L "${current}" ]]
  rm -f "${current}"
}

_restore_units() {
  local rollback_status=0
  local index unit_name
  for index in "${!enabled_units[@]}"; do
    unit_name="$(basename "${enabled_units[$index]}")"
    if [[ "${initial_active[$index]}" == "1" ]]; then
      systemctl restart "${unit_name}" || rollback_status=1
      systemctl is-active --quiet "${unit_name}" || rollback_status=1
    else
      systemctl stop "${unit_name}" || rollback_status=1
    fi
  done
  return "${rollback_status}"
}

_probe_slot() {
  local unit_name="$1"
  local mode="$2"
  local slot_id slot_number slot_port slot_env bridge_token
  local -a probe_command=("${TIMEOUT_BIN}" 240 "${PYTHON_BIN}" -E -s "${PROBE_SCRIPT}")
  [[ "${mode}" != "observe" ]] || probe_command+=(--observe-runtime)
  slot_id="${unit_name#tgyunying-antigravity-}"
  slot_id="${slot_id%.service}"
  slot_number="${slot_id#slot-}"
  slot_port="$((18100 + 10#${slot_number}))"
  slot_env="${BASE_DIR}/shared/antigravity/${slot_id}/provider.env"
  bridge_token="$(sed -n 's/^ANTIGRAVITY_BRIDGE_TOKEN=//p' "${slot_env}")"
  [[ -n "${bridge_token}" ]] || return 1
  ANTIGRAVITY_BRIDGE_TOKEN="${bridge_token}" \
  ANTIGRAVITY_BRIDGE_URL="http://${docker_gateway}:${slot_port}" \
  ANTIGRAVITY_SLOT_ID="${slot_id}" RELEASE_SHA="${release_sha}" \
    "${probe_command[@]}"
}

_restart_and_probe() {
  local release_sha="$1"
  local docker_gateway="$2"
  local slot_unit unit_name
  for slot_unit in "${enabled_units[@]}"; do
    unit_name="$(basename "${slot_unit}")"
    systemctl restart "${unit_name}" || return
    systemctl is-active --quiet "${unit_name}" || return
    systemctl is-failed --quiet "${unit_name}" && return 1
    _probe_slot "${unit_name}" generate || return
  done
}

_runtime_content_unchanged() {
  local target runtime_file runtime_path
  [[ -L "${RUNTIME_ROOT}/current" ]] || return 1
  target="$(readlink -f "${RUNTIME_ROOT}/current")"
  [[ -d "${target}" ]] || return 1
  [[ "$(stat -c '%U:%G' "${target}")" == "root:root" ]] || return 1
  [[ "$(stat -c '%a' "${target}")" == "755" ]] || return 1
  for runtime_file in "${RUNTIME_FILES[@]}"; do
    runtime_path="${target}/${runtime_file}"
    [[ -f "${runtime_path}" && ! -L "${runtime_path}" ]] || return 1
    [[ "$(stat -c '%U:%G' "${runtime_path}")" == "root:root" ]] || return 1
    [[ "$(stat -c '%a' "${runtime_path}")" == "644" ]] || return 1
    cmp -s "${RELEASE_DIR}/backend/scripts/${runtime_file}" "${runtime_path}" || return 1
  done
}

_observe_existing_slots() {
  local slot_unit
  for slot_unit in "${enabled_units[@]}"; do
    _probe_slot "$(basename "${slot_unit}")" observe || return
  done
}

if [[ "${ANTIGRAVITY_SLOT_LOCK_HELD:-0}" != "1" ]]; then
  exec 9>"${LOCK_FILE}"
  flock -n 9 || {
    echo "ANTIGRAVITY_SLOT_OPERATION_LOCKED=${LOCK_FILE}" >&2
    exit 1
  }
fi

enabled_output="$(bash "${PLAN_SCRIPT}")"
[[ -n "${enabled_output}" ]] || exit 0
enabled_units=()
while IFS= read -r slot_unit; do
  [[ -n "${slot_unit}" ]] && enabled_units+=("${slot_unit}")
done <<<"${enabled_output}"

initial_active=()
for slot_unit in "${enabled_units[@]}"; do
  if systemctl is-active --quiet "$(basename "${slot_unit}")"; then
    initial_active+=("1")
  else
    initial_active+=("0")
  fi
done
previous_runtime_target=""
if [[ -L "${RUNTIME_ROOT}/current" ]]; then
  previous_runtime_target="$(readlink "${RUNTIME_ROOT}/current")"
elif [[ -e "${RUNTIME_ROOT}/current" ]]; then
  echo "ANTIGRAVITY_RUNTIME_CURRENT_INVALID=${RUNTIME_ROOT}/current" >&2
  exit 1
fi

release_sha="$(sed -n 's/^RELEASE_SHA=//p' "${RELEASE_DIR}/.image.env")"
docker_gateway="$(docker network inspect "${INFRA_NETWORK_NAME}" --format '{{(index .IPAM.Config 0).Gateway}}')"
[[ -n "${docker_gateway}" ]]
if _runtime_content_unchanged && [[ " ${initial_active[*]} " != *" 0 "* ]]; then
  _observe_existing_slots
  echo "ANTIGRAVITY_RUNTIME_ACTION=preserved_unchanged runtime=${previous_runtime_target} application_sha=${release_sha}"
  exit 0
fi
if SOURCE_DIR="${RELEASE_DIR}/backend/scripts" RELEASE_SHA="${release_sha}" \
  ANTIGRAVITY_RUNTIME_ROOT="${RUNTIME_ROOT}" bash "${RUNTIME_INSTALLER}" \
  && _restart_and_probe "${release_sha}" "${docker_gateway}"; then
  exit 0
fi

rollback_status=0
_restore_runtime "${previous_runtime_target}" || rollback_status=1
_restore_units || rollback_status=1
if (( rollback_status == 0 )); then
  echo "ANTIGRAVITY_SLOT_ROLLBACK=complete units=${#enabled_units[@]}" >&2
else
  echo "ANTIGRAVITY_SLOT_ROLLBACK=failed units=${#enabled_units[@]}" >&2
fi
exit 1
