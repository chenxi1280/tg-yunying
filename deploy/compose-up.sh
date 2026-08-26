#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/docker-env.sh"

ensure_runtime_env

echo "==> Release directory: $APP_DIR"
echo "==> Compose file: $COMPOSE_FILE"
echo "==> Env file: $ENV_FILE"

VERIFICATION_FENCED_CONTAINER_ID=""

docker_login_ghcr() {
  if [[ "$TGYUNYING_BACKEND_IMAGE" != ghcr.io/* \
    && "$TGYUNYING_FRONTEND_IMAGE" != ghcr.io/* \
    && "${TGYUNYING_IMAGE_VERIFICATION_IMAGE:-}" != ghcr.io/* ]]; then
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

fence_image_verification_restart() {
  if ! verification_remote_enabled; then
    return 0
  fi

  local container_id
  container_id="$(compose ps -q image-verification-worker)"
  if [[ -z "$container_id" ]]; then
    return 0
  fi

  docker inspect "$container_id" >/dev/null
  docker update --restart=no "$container_id" >/dev/null
  VERIFICATION_FENCED_CONTAINER_ID="$container_id"
  echo "Fenced image verification restart policy: ${container_id}"
}

restore_image_verification_restart() {
  if [[ -z "$VERIFICATION_FENCED_CONTAINER_ID" ]]; then
    return 0
  fi

  docker inspect "$VERIFICATION_FENCED_CONTAINER_ID" >/dev/null
  docker update --restart=unless-stopped "$VERIFICATION_FENCED_CONTAINER_ID" >/dev/null
  echo "Restored image verification restart policy: ${VERIFICATION_FENCED_CONTAINER_ID}"
  VERIFICATION_FENCED_CONTAINER_ID=""
}

assert_fenced_image_verification_stopped() {
  if [[ -z "$VERIFICATION_FENCED_CONTAINER_ID" ]]; then
    return 0
  fi

  local status
  status="$(docker inspect "$VERIFICATION_FENCED_CONTAINER_ID" --format '{{.State.Status}}')"
  if [[ "$status" == "running" || "$status" == "restarting" ]]; then
    echo "Image verification worker remained active after fencing: ${VERIFICATION_FENCED_CONTAINER_ID} status=$status" >&2
    return 1
  fi
}

assert_single_image_verification_runtime() {
  local current_id cmdline_path cmdline cgroup runtime_id existing seen
  local runtime_ids=()
  current_id="$(docker inspect tgyunying-image-verification-worker --format '{{.Id}}')"

  for cmdline_path in /proc/[0-9]*/cmdline; do
    cmdline="$(tr '\0' ' ' < "$cmdline_path" 2>/dev/null || true)"
    [[ "$cmdline" == *"app.image_verification_worker_app:app"* ]] || continue
    cgroup="$(cat "${cmdline_path%/cmdline}/cgroup" 2>/dev/null || true)"
    runtime_id="$(grep -oE 'docker[-/][0-9a-f]{64}' <<< "$cgroup" | head -n 1 | cut -c 8- || true)"
    if [[ -z "$runtime_id" ]]; then
      echo "Image verification runtime has no Docker cgroup identity: ${cmdline_path}" >&2
      return 1
    fi
    seen=false
    for existing in "${runtime_ids[@]}"; do
      [[ "$existing" == "$runtime_id" ]] && seen=true
    done
    [[ "$seen" == true ]] || runtime_ids+=("$runtime_id")
  done

  if (( ${#runtime_ids[@]} != 1 )) || [[ "${runtime_ids[0]:-}" != "$current_id" ]]; then
    echo "Image verification runtime inventory mismatch: current=$current_id runtimes=${runtime_ids[*]:-none}" >&2
    return 1
  fi
  echo "Image verification runtime inventory verified: current=$current_id count=1"
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
  echo "==> Pruning stopped containers, build cache, and dangling images before image pull"
  docker container prune -f
  docker builder prune -af
  docker image prune -f
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
)

WORKER_SERVICES=(
  worker-authorization-abc-sweep
  worker-planner
  worker-ai-generation
  worker-ai-generation-2
  worker-ai-generation-3
  worker-dispatcher-1
  worker-dispatcher-2
  worker-search-dispatcher
  worker-listener
  worker-recovery
  worker-account-security
  worker-material-cache
  worker-voice-profile
  worker-account-online
  worker-ai-memory
  worker-metrics
)

if [[ "${ACCOUNT_BATCH_LOGIN_MODE:-off}" != "off" ]]; then
  WORKER_SERVICES+=(worker-account-login)
fi

if verification_remote_enabled; then
  WORKER_SERVICES=(image-verification-worker "${WORKER_SERVICES[@]}")
fi

prune_docker_pull_cache

echo "==> Pulling shared backend runtime image"
compose pull "${BACKEND_SERVICES[@]}"

if verification_remote_enabled; then
  echo "==> Pulling image verification worker image"
  compose pull image-verification-worker
fi

echo "==> Pulling frontend static image"
docker pull "$TGYUNYING_FRONTEND_IMAGE"

publish_frontend_static "$TGYUNYING_FRONTEND_IMAGE"

# ===== Stage A: fence business writers — stop old workers, apply migrations, retire heartbeats, stage dispatch contract =====
echo "==> Fencing old workers before migration and contract-version switch"
fence_image_verification_restart
trap restore_image_verification_restart EXIT
compose stop "${WORKER_SERVICES[@]}"
assert_fenced_image_verification_stopped
restore_image_verification_restart
trap - EXIT
workers_stopped_before="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"

echo "==> Starting backend and applying migrations"
compose up -d --no-build --remove-orphans "${BACKEND_SERVICES[@]}"
wait_for_container_ready tgyunying-backend "${TGYUNYING_BACKEND_READY_TIMEOUT_SECONDS:-180}"

release_version="${STATIC_RELEASE_ID:-$(basename "$APP_DIR")}"
release_actor="${SHARED_DISPATCH_RELEASE_ACTOR:-github-actions-deploy}"
approval_ref="${SHARED_DISPATCH_APPROVAL_REF:-release:${release_version}}"

echo "==> Retiring heartbeats for compose workers confirmed stopped"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract retire-stopped-writers \
  --actor "$release_actor" \
  --approval-ref "$approval_ref" \
  --stopped-before "$workers_stopped_before"

echo "==> Staging shared dispatch candidate contract"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract stage \
  --actor "$release_actor" \
  --approval-ref "$approval_ref"

echo "==> Starting new workers in fenced readiness"
compose up -d --no-build --remove-orphans "${WORKER_SERVICES[@]}"
wait_for_container_ready \
  tgyunying-worker-dispatcher-1 \
  "${TGYUNYING_WORKER_READY_TIMEOUT_SECONDS:-180}"
wait_for_container_ready \
  tgyunying-worker-dispatcher-2 \
  "${TGYUNYING_WORKER_READY_TIMEOUT_SECONDS:-180}"
wait_for_container_ready \
  tgyunying-worker-search-dispatcher \
  "${TGYUNYING_WORKER_READY_TIMEOUT_SECONDS:-180}"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract verify-ready

# ===== Stage B: zero-business-writer window — contract staged but NOT active; recover claims, then all-task fulfillment takeover (sole automatic owner) and AI content scope takeover must complete here =====
echo "==> Recovering fenced claims and reconciling dispatch ledgers"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract reconcile-ledger \
  --actor "$release_actor" \
  --approval-ref "$approval_ref"

echo "==> Taking over active tasks while all business writers remain fenced"
docker exec -i tgyunying-backend \
  python -m scripts.takeover_all_task_fulfillment
docker exec -i tgyunying-backend \
  python -m scripts.takeover_all_task_fulfillment --apply

echo "==> Previewing and applying AI content scope takeover"
takeover_preview="$(docker exec -i tgyunying-backend \
  python -m scripts.takeover_ai_content_scope preview \
  --actor "$release_actor" \
  --approval-ref "$approval_ref" \
  --release-version "$release_version" \
  --config-version "$DISPATCH_REBUILD_CONTRACT_VERSION")"
echo "$takeover_preview"
takeover_fields="$(printf '%s' "$takeover_preview" | docker exec -i tgyunying-backend \
  python -c 'import json,sys; value=json.load(sys.stdin); print(value["batch_id"], value["classification_hash"], json.dumps(value["classification_counts"], separators=(",", ":")))')"
read -r takeover_batch_id takeover_hash takeover_counts <<< "$takeover_fields"
docker exec -i tgyunying-backend \
  python -m scripts.takeover_ai_content_scope apply \
  --batch-id "$takeover_batch_id" \
  --classification-hash "$takeover_hash" \
  --expected-counts-json "$takeover_counts" \
  --actor "$release_actor" \
  --approval-ref "$approval_ref"

# ===== Stage C: activate dispatch contract and verify — business writers resume; post-release jobs may only run read-only verify-active =====
echo "==> Activating shared dispatch contract after takeover closure"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract activate \
  --actor "$release_actor" \
  --approval-ref "$approval_ref" \
  --takeover-head-batch-id "$takeover_batch_id"

echo "==> Verifying active shared dispatch contract and ledgers"
docker exec -i tgyunying-backend \
  python -m scripts.manage_shared_dispatch_contract verify-active

if verification_remote_enabled; then
  wait_for_container_ready \
    tgyunying-image-verification-worker \
    "${IMAGE_VERIFICATION_WORKER_READY_TIMEOUT_SECONDS:-180}"
  assert_single_image_verification_runtime
fi

echo "==> Container status"
compose ps
