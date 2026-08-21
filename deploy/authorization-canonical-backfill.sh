#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 --mode preview|apply|status|qualify-preview|qualify-apply --tenant-id ID [options]" >&2
  exit 2
fi

exec docker exec "$CONTAINER_NAME" python scripts/authorization_canonical_backfill.py "$@"
