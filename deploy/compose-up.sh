#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/docker-env.sh"

ensure_runtime_env

echo "==> Release directory: $APP_DIR"
echo "==> Compose file: $COMPOSE_FILE"
echo "==> Env file: $ENV_FILE"

docker_login_ghcr() {
  if [[ "$TGYUNYING_BACKEND_IMAGE" != ghcr.io/* && "$TGYUNYING_FRONTEND_IMAGE" != ghcr.io/* ]]; then
    return 0
  fi

  if [[ -z "${GHCR_USERNAME:-}" || -z "${GHCR_TOKEN:-}" ]]; then
    echo "GHCR_USERNAME and GHCR_TOKEN are required to pull GHCR images." >&2
    exit 1
  fi

  printf '%s\n' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin >/dev/null
}

wait_for_container_ready() {
  local container_name="$1"
  local timeout_seconds="${2:-180}"
  local started_at
  started_at="$(date +%s)"

  while true; do
    local now elapsed status health
    now="$(date +%s)"
    elapsed=$((now - started_at))
    status="$(docker inspect "$container_name" --format '{{.State.Status}}' 2>/dev/null || true)"
    health="$(docker inspect "$container_name" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"

    if [[ "$status" == "running" && ( -z "$health" || "$health" == "healthy" ) ]]; then
      echo "Container ready: ${container_name} status=$status health=${health:-none}"
      return 0
    fi

    if [[ "$status" == "exited" || "$status" == "dead" || "$health" == "unhealthy" ]]; then
      echo "Service failed: ${container_name} status=${status:-unknown} health=${health:-none}" >&2
      docker logs --tail 200 "$container_name" >&2 || true
      return 1
    fi

    if (( elapsed >= timeout_seconds )); then
      echo "Timed out waiting for ${container_name}: status=${status:-unknown} health=${health:-none}" >&2
      docker logs --tail 200 "$container_name" >&2 || true
      return 1
    fi

    sleep 5
  done
}

prune_static_releases() {
  local releases_dir="$1"
  local current_link="$2"
  local keep="${3:-5}"
  mapfile -t release_paths < <(find "$releases_dir" -mindepth 1 -maxdepth 1 -type d | sort)
  local total="${#release_paths[@]}"

  if (( total <= keep )); then
    return 0
  fi

  local current_target=""
  if [[ -L "$current_link" ]]; then
    current_target="$(readlink -f "$current_link")"
  fi

  local remove_count=$(( total - keep ))
  local idx=0
  while (( idx < remove_count )); do
    if [[ "${release_paths[$idx]}" != "$current_target" ]]; then
      rm -rf "${release_paths[$idx]}"
    fi
    idx=$((idx + 1))
  done
}

prune_docker_pull_cache() {
  echo "==> Docker disk usage before image pull"
  docker system df || true
  echo "==> Pruning stopped containers and unused image cache before image pull"
  docker container prune -f
  docker builder prune -af
  docker image prune -af
  echo "==> Docker disk usage after image cache prune"
  docker system df || true
}

preserve_frontend_assets() {
  local releases_dir="$1"
  local tmp_dir="$2"
  local preserved_assets=()

  if [[ ! -d "$releases_dir" ]]; then
    return 0
  fi

  mapfile -t preserved_assets < <(find "$releases_dir" -mindepth 2 -maxdepth 2 -type d -name assets ! -path "${tmp_dir}/assets" | sort)
  if (( ${#preserved_assets[@]} == 0 )); then
    return 0
  fi

  echo "==> Preserving frontend assets from ${#preserved_assets[@]} previous release(s)"
  mkdir -p "${tmp_dir}/assets"
  local asset_dir
  for asset_dir in "${preserved_assets[@]}"; do
    cp -a "${asset_dir}/." "${tmp_dir}/assets/"
  done
}

publish_frontend_static() {
  local image="$1"
  local base_dir="${TGYUNYING_FRONTEND_STATIC_BASE_DIR:-/data/infra/www/${TGYUNYING_WEB_HOST:-tgyunying}}"
  local release_id="${STATIC_RELEASE_ID:-$(basename "$APP_DIR")}"
  local keep="${STATIC_KEEP_RELEASES:-5}"
  local html_dir="/usr/share/nginx/html"
  local releases_dir="${base_dir}/releases"
  local release_dir="${releases_dir}/${release_id}"
  local tmp_dir="${release_dir}.tmp"
  local current_link="${base_dir}/current"
  local container_id=""

  echo "==> Publishing frontend static files: ${image} -> ${release_dir}"
  mkdir -p "$releases_dir"
  rm -rf "$tmp_dir"
  mkdir -p "$tmp_dir"
  preserve_frontend_assets "$releases_dir" "$tmp_dir"

  container_id="$(docker create "$image")"
  cleanup_static_container() {
    if [[ -n "$container_id" ]]; then
      docker rm "$container_id" >/dev/null 2>&1 || true
    fi
  }
  trap 'cleanup_static_container; trap - RETURN' RETURN

  docker cp "${container_id}:${html_dir}/." "$tmp_dir/"
  test -f "${tmp_dir}/index.html"

  cleanup_static_container
  container_id=""
  trap - RETURN

  rm -rf "$release_dir"
  mv "$tmp_dir" "$release_dir"
  ln -sfn "$release_dir" "${current_link}.tmp"
  mv -Tf "${current_link}.tmp" "$current_link"
  prune_static_releases "$releases_dir" "$current_link" "$keep"
}

docker_login_ghcr

BACKEND_SERVICES=(
  backend
  worker-planner
  worker-dispatcher-1
  worker-dispatcher-2
  worker-dispatcher-3
  worker-dispatcher-4
  worker-listener
  worker-recovery
  worker-account-security
  worker-metrics
)

prune_docker_pull_cache

echo "==> Pulling backend image"
compose pull "${BACKEND_SERVICES[@]}"

echo "==> Pulling frontend static image"
docker pull "$TGYUNYING_FRONTEND_IMAGE"

publish_frontend_static "$TGYUNYING_FRONTEND_IMAGE"

echo "==> Starting backend and workers"
compose up -d --no-build --remove-orphans "${BACKEND_SERVICES[@]}"
wait_for_container_ready tgyunying-backend "${TGYUNYING_BACKEND_READY_TIMEOUT_SECONDS:-180}"

echo "==> Container status"
compose ps
