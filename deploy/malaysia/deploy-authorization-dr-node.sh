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
required_env_file_names=(
  AUTHORIZATION_DR_CONTROL_PLANE_URL
  AUTHORIZATION_DR_INTERNAL_TOKEN
  AUTHORIZATION_DR_EXPECTED_EGRESS_IP
  MY_WAKE_STORAGE_MODE
)
for name in "${required_env_file_names[@]}"; do
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)"
  if [[ -z "$value" || "$value" == replace-with-* ]]; then
    echo "$name must be configured in $ENV_FILE" >&2
    exit 1
  fi
done
storage_mode="$(sed -n 's/^MY_WAKE_STORAGE_MODE=//p' "$ENV_FILE" | tail -n 1)"
case "$storage_mode" in
  ssh_mirror)
    storage_names=(
      MY_WAKE_SNAPSHOT_PREFIX
      MY_WAKE_SSH_HOST
      MY_WAKE_SSH_PORT
      MY_WAKE_SSH_USER
      MY_WAKE_SSH_IDENTITY_FILE
      MY_WAKE_SSH_KNOWN_HOSTS_FILE
      MY_WAKE_SSH_REMOTE_DIR
      MY_WAKE_RECOVERY_KEY_FILE
    )
    ;;
  kms_oss)
    storage_names=(
      MY_WAKE_OSS_ENDPOINT
      MY_WAKE_OSS_BUCKET
      MY_WAKE_OSS_ACCESS_KEY_ID
      MY_WAKE_OSS_ACCESS_KEY_SECRET
      MY_WAKE_OSS_PREFIX
      MY_WAKE_KMS_ENDPOINT
      MY_WAKE_KMS_REGION_ID
      MY_WAKE_KMS_ACCESS_KEY_ID
      MY_WAKE_KMS_ACCESS_KEY_SECRET
      MY_WAKE_KMS_KEY_ID
    )
    ;;
  *)
    echo "MY_WAKE_STORAGE_MODE must be ssh_mirror or kms_oss" >&2
    exit 1
    ;;
esac
for name in "${storage_names[@]}"; do
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)"
  if [[ -z "$value" || "$value" == replace-with-* ]]; then
    echo "$name must be configured in $ENV_FILE" >&2
    exit 1
  fi
done
if [[ "$storage_mode" == "ssh_mirror" ]]; then
  secret_dir="${MY_WAKE_SECRET_HOST_DIR:-/opt/tgyunying-authorization-dr/secrets}"
  for file in id_ed25519 known_hosts recovery.key; do
    if [[ ! -s "$secret_dir/$file" ]]; then
      echo "SSH mirror secret is missing: $secret_dir/$file" >&2
      exit 1
    fi
  done
  ssh_host="$(sed -n 's/^MY_WAKE_SSH_HOST=//p' "$ENV_FILE" | tail -n 1)"
  ssh_port="$(sed -n 's/^MY_WAKE_SSH_PORT=//p' "$ENV_FILE" | tail -n 1)"
  ssh_user="$(sed -n 's/^MY_WAKE_SSH_USER=//p' "$ENV_FILE" | tail -n 1)"
  remote_dir="$(sed -n 's/^MY_WAKE_SSH_REMOTE_DIR=//p' "$ENV_FILE" | tail -n 1)"
  printf -v remote_access_check \
    'test -d %q && test -x %q && test -w %q' \
    "$remote_dir" "$remote_dir" "$remote_dir"
  ssh \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$secret_dir/known_hosts" \
    -o ConnectTimeout=15 \
    -i "$secret_dir/id_ed25519" \
    -p "$ssh_port" \
    "$ssh_user@$ssh_host" \
    "$remote_access_check"
fi
mkdir -p "$MY_WAKE_BUNDLE_LOCAL_HOST_DIR"
export AUTHORIZATION_DR_ENV_FILE="$ENV_FILE"
image_mode="${AUTHORIZATION_DR_IMAGE_MODE:-registry}"
case "$image_mode" in
  registry)
    docker compose -f "$COMPOSE_FILE" pull authorization-dr-node
    ;;
  local)
    if ! docker image inspect "$TGYUNYING_BACKEND_IMAGE" >/dev/null 2>&1; then
      echo "Local authorization DR image is missing: $TGYUNYING_BACKEND_IMAGE" >&2
      exit 1
    fi
    ;;
  *)
    echo "AUTHORIZATION_DR_IMAGE_MODE must be registry or local" >&2
    exit 1
    ;;
esac
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
