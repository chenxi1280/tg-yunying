#!/usr/bin/env bash
set -euo pipefail

UNIT_DIR="${ANTIGRAVITY_SLOT_UNIT_DIR:-/etc/systemd/system}"
shopt -s nullglob
slot_units=("${UNIT_DIR}"/tgyunying-antigravity-slot-*.service)
shopt -u nullglob

for slot_unit in "${slot_units[@]}"; do
  unit_name="$(basename "${slot_unit}")"
  if systemctl is-enabled --quiet "${unit_name}"; then
    echo "${slot_unit}"
    continue
  fi
  if systemctl is-active --quiet "${unit_name}"; then
    echo "ANTIGRAVITY_SLOT_DRIFT=${unit_name}:disabled_active" >&2
    exit 1
  fi
  echo "ANTIGRAVITY_SLOT_SKIPPED=${unit_name}:disabled_inactive" >&2
done
