#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"
docker exec "$CONTAINER_NAME" python /app/scripts/authorization_canonical_backfill.py "$@"
