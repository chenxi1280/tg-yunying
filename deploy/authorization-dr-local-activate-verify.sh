#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${TGYUNYING_BACKEND_CONTAINER:-tgyunying-backend}"

exec docker exec "$CONTAINER_NAME" python scripts/authorization_dr_local_activate_verify.py "$@"
