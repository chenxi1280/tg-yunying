#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${AUTHORIZATION_DR_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.authorization-dr.yml}"
ENV_FILE="${AUTHORIZATION_DR_ENV_FILE:-/opt/tgyunying-authorization-dr/node.env}"

required=(
  TGYUNYING_BACKEND_IMAGE
  MY_WAKE_BUNDLE_LOCAL_HOST_DIR
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required" >&2
    exit 1
  fi
done
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Authorization DR env file does not exist: $ENV_FILE" >&2
  exit 1
fi
mkdir -p "$MY_WAKE_BUNDLE_LOCAL_HOST_DIR"
export AUTHORIZATION_DR_ENV_FILE="$ENV_FILE"
docker compose -f "$COMPOSE_FILE" pull authorization-dr-node
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans authorization-dr-node

container_id="$(docker compose -f "$COMPOSE_FILE" ps -q authorization-dr-node)"
if [[ -z "$container_id" ]]; then
  echo "Authorization DR node container was not created" >&2
  exit 1
fi
for _ in {1..24}; do
  status="$(docker inspect "$container_id" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}')"
  if [[ "$status" == "running/healthy" ]]; then
    echo "Authorization DR node is ready: $status"
    exit 0
  fi
  if [[ "$status" == exited/* || "$status" == dead/* || "$status" == running/unhealthy ]]; then
    docker logs --tail 100 "$container_id" >&2
    exit 1
  fi
  sleep 5
done
docker logs --tail 100 "$container_id" >&2
echo "Authorization DR node readiness timed out" >&2
exit 1
