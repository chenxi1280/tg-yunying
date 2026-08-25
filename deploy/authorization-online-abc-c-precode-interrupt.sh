#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

MODE=""
PREVIOUS=""
for ARGUMENT in "$@"; do
  if [[ "$PREVIOUS" == "--mode" ]]; then
    MODE="$ARGUMENT"
    break
  fi
  PREVIOUS="$ARGUMENT"
done

if [[ "$MODE" == "apply" ]] && docker top "$CONTAINER_NAME" -eo pid,args | grep -Fq "authorization_online_abc_runner.py"; then
  echo "online_abc_c_precode_interrupt_runner_present: reconcile the existing runner first" >&2
  exit 1
fi

exec docker exec "$CONTAINER_NAME" python scripts/authorization_online_abc_c_precode_interrupt.py "$@"
