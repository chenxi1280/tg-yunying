#!/usr/bin/env bash

set -euo pipefail

USER_NAME="${USER_NAME:-root}"
HOST="${HOST:-}"
BASE_DIR="${BASE_DIR:-/data/tgyunying}"
REF_NAME="${REF_NAME:-HEAD}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
KEEP_ARCHIVE="${KEEP_ARCHIVE:-0}"
EXPECTED_BRANCHES="${EXPECTED_BRANCHES:-release}"
RELEASE_SSH_ATTEMPTS="${RELEASE_SSH_ATTEMPTS:-3}"
RELEASE_SSH_RETRY_DELAY="${RELEASE_SSH_RETRY_DELAY:-10}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:-ghcr.io/chenxi1280}"
STATIC_KEEP_RELEASES="${STATIC_KEEP_RELEASES:-5}"
SSH_OPTS=(
  -o "ConnectTimeout=${SSH_CONNECT_TIMEOUT:-20}"
  -o "ServerAliveInterval=${SSH_SERVER_ALIVE_INTERVAL:-30}"
  -o "ServerAliveCountMax=${SSH_SERVER_ALIVE_COUNT_MAX:-10}"
  -o "TCPKeepAlive=yes"
)

usage() {
  cat <<'EOF'
Usage:
  bash deploy/release.sh --host <host> [options]

Options:
  --host HOST           Target host, required
  --user USER           SSH user, default root
  --base-dir DIR        Remote base directory, default /data/tgyunying
  --ref REF             Git ref to release, default HEAD
  --allow-dirty         Allow releasing with local uncommitted changes
  --branch-list "..."   Allowed release branches, default "release"
  --ssh-opt OPT         Extra ssh/scp option, can be repeated
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --user)
      USER_NAME="$2"
      shift 2
      ;;
    --base-dir)
      BASE_DIR="$2"
      shift 2
      ;;
    --ref)
      REF_NAME="$2"
      shift 2
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    --branch-list)
      EXPECTED_BRANCHES="$2"
      shift 2
      ;;
    --ssh-opt)
      SSH_OPTS+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing command: $cmd" >&2
    exit 1
  fi
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "${name} must be a positive integer, got: ${value}" >&2
    exit 1
  fi
}

run_with_retries() {
  local label="$1"
  shift

  local attempt status
  for ((attempt = 1; attempt <= RELEASE_SSH_ATTEMPTS; attempt++)); do
    if ((RELEASE_SSH_ATTEMPTS > 1)); then
      echo "==> ${label} (attempt ${attempt}/${RELEASE_SSH_ATTEMPTS})"
    fi

    if "$@"; then
      return 0
    else
      status=$?
    fi

    if ((attempt == RELEASE_SSH_ATTEMPTS)); then
      echo "${label} failed after ${RELEASE_SSH_ATTEMPTS} attempt(s)" >&2
      return "$status"
    fi

    echo "${label} failed with exit code ${status}; retrying in ${RELEASE_SSH_RETRY_DELAY}s" >&2
    sleep "$RELEASE_SSH_RETRY_DELAY"
  done
}

if [[ -z "$HOST" ]]; then
  usage >&2
  exit 1
fi

require_command git
require_command ssh
require_command scp
require_command mktemp
require_command tar
require_positive_integer RELEASE_SSH_ATTEMPTS "$RELEASE_SSH_ATTEMPTS"
require_positive_integer RELEASE_SSH_RETRY_DELAY "$RELEASE_SSH_RETRY_DELAY"

current_branch="$(git branch --show-current)"
if [[ -z "$current_branch" ]]; then
  echo "Cannot detect current git branch" >&2
  exit 1
fi

branch_allowed=0
for branch in $EXPECTED_BRANCHES; do
  if [[ "$current_branch" == "$branch" ]]; then
    branch_allowed=1
    break
  fi
done

if [[ "$branch_allowed" != "1" ]]; then
  echo "Refusing to release from branch '${current_branch}'. Allowed branches: ${EXPECTED_BRANCHES}" >&2
  exit 1
fi

if [[ "$ALLOW_DIRTY" != "1" ]] && [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty. Commit or stash changes first, or rerun with --allow-dirty." >&2
  exit 1
fi

short_sha="$(git rev-parse --short "$REF_NAME")"
full_sha="$(git rev-parse "$REF_NAME")"
image_tag="${IMAGE_TAG:-$full_sha}"
TGYUNYING_BACKEND_IMAGE="${TGYUNYING_BACKEND_IMAGE:-${IMAGE_NAMESPACE}/tg-yunying-backend:${image_tag}}"
TGYUNYING_FRONTEND_IMAGE="${TGYUNYING_FRONTEND_IMAGE:-${IMAGE_NAMESPACE}/tg-yunying-frontend:${image_tag}}"
release_id="$(date '+%Y%m%d%H%M%S')_${short_sha}"
archive_path="$(mktemp "/tmp/tgyunying-release-${release_id}.XXXXXX.tar.gz")"
image_env_path="$(mktemp "/tmp/tgyunying-image-env-${release_id}.XXXXXX.env")"
remote_archive="${BASE_DIR}/incoming/${release_id}.tar.gz"
remote_tmp_archive="/tmp/tgyunying-release-${release_id}.tar.gz"
remote_image_env="/tmp/tgyunying-release-${release_id}.image.env"
remote_release_dir="${BASE_DIR}/releases/${release_id}"

trap '[[ "$KEEP_ARCHIVE" == "1" ]] || rm -f "$archive_path" "$image_env_path"' EXIT

cat >"$image_env_path" <<EOF
TGYUNYING_BACKEND_IMAGE=${TGYUNYING_BACKEND_IMAGE}
TGYUNYING_FRONTEND_IMAGE=${TGYUNYING_FRONTEND_IMAGE}
STATIC_RELEASE_ID=${release_id}
STATIC_KEEP_RELEASES=${STATIC_KEEP_RELEASES}
EOF

if [[ -n "${TGYUNYING_FRONTEND_STATIC_BASE_DIR:-}" ]]; then
  printf 'TGYUNYING_FRONTEND_STATIC_BASE_DIR=%s\n' "$TGYUNYING_FRONTEND_STATIC_BASE_DIR" >>"$image_env_path"
fi
if [[ -n "${TGYUNYING_WEB_HOST:-}" ]]; then
  printf 'TGYUNYING_WEB_HOST=%s\n' "$TGYUNYING_WEB_HOST" >>"$image_env_path"
fi

shell_quote() {
  printf '%q' "$1"
}

remote_env_prefix=""

append_remote_env_if_set() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$value" ]]; then
    remote_env_prefix+=" ${name}=$(shell_quote "$value")"
  fi
}

append_remote_env_if_set GHCR_USERNAME
append_remote_env_if_set GHCR_TOKEN
append_remote_env_if_set POST_DEPLOY_CHECKS_ENABLED
append_remote_env_if_set TGYUNYING_CHECK_HOST_NGINX
append_remote_env_if_set TGYUNYING_CHECK_PUBLIC_URLS
append_remote_env_if_set TGYUNYING_CHECK_ATTEMPTS
append_remote_env_if_set TGYUNYING_CHECK_RETRY_DELAY_SECONDS

echo "==> Creating release archive for ${REF_NAME} (${short_sha})"
git archive --format=tar.gz --output "$archive_path" "$REF_NAME"

echo "==> Uploading release archive to ${USER_NAME}@${HOST}:${remote_tmp_archive}"
run_with_retries "Uploading release archive" \
  scp "${SSH_OPTS[@]}" "$archive_path" "${USER_NAME}@${HOST}:${remote_tmp_archive}"
run_with_retries "Uploading image env" \
  scp "${SSH_OPTS[@]}" "$image_env_path" "${USER_NAME}@${HOST}:${remote_image_env}"

echo "==> Installing release ${release_id} on ${HOST}"
run_with_retries "Installing remote release" \
  ssh "${SSH_OPTS[@]}" "${USER_NAME}@${HOST}" "\
set -euo pipefail && \
mkdir -p '${BASE_DIR}/incoming' '${BASE_DIR}/releases' && \
existing_image_env='' && \
if [[ -f '${remote_release_dir}/.image.env' ]]; then \
  existing_image_env=\"\$(mktemp '/tmp/tgyunying-existing-image-env.XXXXXX')\" && \
  cp '${remote_release_dir}/.image.env' \"\${existing_image_env}\"; \
fi && \
if [[ -f '${remote_tmp_archive}' ]]; then mv -f '${remote_tmp_archive}' '${remote_archive}'; fi && \
if [[ -f '${remote_archive}' ]]; then \
  rm -rf '${remote_release_dir}' && \
  mkdir -p '${remote_release_dir}' && \
  tar -xzf '${remote_archive}' -C '${remote_release_dir}'; \
fi && \
test -d '${remote_release_dir}' && \
if [[ -f '${remote_image_env}' ]]; then \
  mv -f '${remote_image_env}' '${remote_release_dir}/.image.env'; \
elif [[ -n \"\${existing_image_env}\" && -f \"\${existing_image_env}\" ]]; then \
  mv -f \"\${existing_image_env}\" '${remote_release_dir}/.image.env'; \
elif [[ -f '${remote_release_dir}/.image.env' ]]; then \
  :; \
else \
  echo 'Missing release image env: ${remote_image_env}' >&2; \
  exit 1; \
fi && \
${remote_env_prefix} bash '${remote_release_dir}/deploy/server-install-release.sh' \
  --base-dir '${BASE_DIR}' \
  --release-dir '${remote_release_dir}' \
  --release-id '${release_id}' && \
rm -f '${remote_archive}' '${remote_tmp_archive}' '${remote_image_env}'"

echo "Release ${release_id} completed"
