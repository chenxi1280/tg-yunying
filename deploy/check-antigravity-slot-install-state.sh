#!/usr/bin/env bash
set -euo pipefail

unit_name="${1:?unit name is required}"
unit_state="$(systemctl show "${unit_name}" -p LoadState -p ActiveState -p MainPID)"
load_state="$(sed -n 's/^LoadState=//p' <<<"${unit_state}")"
active_state="$(sed -n 's/^ActiveState=//p' <<<"${unit_state}")"
main_pid="$(sed -n 's/^MainPID=//p' <<<"${unit_state}")"

if [[ "${load_state}" == "not-found" && "${main_pid}" == "0" ]]; then
  exit 0
fi
if [[ "${load_state}" == "loaded" && "${active_state}" == "inactive" \
  && "${main_pid}" == "0" ]]; then
  exit 0
fi
echo "ANTIGRAVITY_SLOT_INSTALL_STATE_INVALID=${unit_name}:${load_state}:${active_state}:${main_pid}" >&2
exit 1
