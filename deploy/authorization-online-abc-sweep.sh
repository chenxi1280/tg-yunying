#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 --mode preview|sweep --until-exhausted --batch-id ID [approval arguments]" >&2
  exit 2
fi

if [[ " $* " == *" --mode worker "* ]]; then
  echo "worker mode is reserved for the compose-managed supervisor" >&2
  exit 2
fi

MODE=""
PREVIOUS=""
for ARGUMENT in "$@"; do
  if [[ "$PREVIOUS" == "--mode" ]]; then
    MODE="$ARGUMENT"
    break
  fi
  PREVIOUS="$ARGUMENT"
done

if [[ "$MODE" == "sweep" || "$MODE" == "apply" ]]; then
  if docker top "$CONTAINER_NAME" -eo pid,args | grep -Fq "authorization_online_abc_runner.py"; then
    echo "online_abc_sweep_runner_present: stop and reconcile the legacy runner first" >&2
    exit 1
  fi
fi

exec docker exec "$CONTAINER_NAME" python scripts/authorization_online_abc_sweep.py "$@"
