#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 --mode preview|apply|start|sync|status [options]" >&2
  exit 2
fi

exec docker exec "$CONTAINER_NAME" python scripts/authorization_online_abc.py "$@"
