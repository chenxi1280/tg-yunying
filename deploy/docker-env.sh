#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="${BASE_DIR:-/data/tgyunying}"
CURRENT_APP_DIR="${BASE_DIR}/current"
LEGACY_APP_DIR="${BASE_DIR}"

if [[ -n "${APP_DIR:-}" ]]; then
  APP_DIR="$APP_DIR"
elif [[ -L "$CURRENT_APP_DIR" || -d "$CURRENT_APP_DIR" ]]; then
  APP_DIR="$CURRENT_APP_DIR"
else
  APP_DIR="$LEGACY_APP_DIR"
fi

SHARED_DIR="${SHARED_DIR:-${BASE_DIR}/shared}"
COMPOSE_FILE="${COMPOSE_FILE:-${APP_DIR}/docker-compose.server.yml}"
IMAGE_ENV_FILE="${IMAGE_ENV_FILE:-${APP_DIR}/.image.env}"

if [[ -n "${ENV_FILE:-}" ]]; then
  ENV_FILE="$ENV_FILE"
elif [[ -f "${SHARED_DIR}/.env" ]]; then
  ENV_FILE="${SHARED_DIR}/.env"
else
  ENV_FILE="${APP_DIR}/.env"
fi

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing command: $cmd" >&2
    exit 1
  fi
}

load_base_env() {
  require_command docker

  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing env file: $ENV_FILE" >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  if [[ -f "$IMAGE_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$IMAGE_ENV_FILE"
  fi
  set +a
}

ensure_runtime_env() {
  load_base_env

  local required=(
    TGYUNYING_BACKEND_IMAGE
    TGYUNYING_FRONTEND_IMAGE
    DATABASE_URL
    REDIS_URL
    SESSION_SECRET_KEY
    CORS_ORIGINS
    ADMIN_BOOTSTRAP_PASSWORD
    PUBLIC_APP_BASE_URL
  )

  local missing=()
  local key
  for key in "${required[@]}"; do
    if [[ -z "${!key:-}" ]]; then
      missing+=("$key")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    echo "Missing runtime env vars: ${missing[*]}" >&2
    exit 1
  fi

  if [[ "${SESSION_SECRET_KEY}" == "dev-only-change-me" || "${SESSION_SECRET_KEY}" == change-me* ]]; then
    echo "SESSION_SECRET_KEY must be replaced before production start." >&2
    exit 1
  fi

  if [[ "${ADMIN_BOOTSTRAP_PASSWORD}" == change-me* || "${ADMIN_BOOTSTRAP_PASSWORD}" == "admin123" ]]; then
    echo "ADMIN_BOOTSTRAP_PASSWORD must be replaced before first production start." >&2
    exit 1
  fi
}

compose() {
  (cd "$APP_DIR" && docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@")
}
