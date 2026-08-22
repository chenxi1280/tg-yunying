#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 --mode status|run|resume --batch-id ID [--requested-by USER --approved-by USER --approval-ref REF]" >&2
  exit 2
fi

exec docker exec "$CONTAINER_NAME" python scripts/authorization_online_abc_runner.py "$@"
