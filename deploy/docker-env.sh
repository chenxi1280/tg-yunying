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

  if is_true "${IMAGE_VERIFICATION_CONTRACT_ENABLED:-false}"; then
    require_runtime_values \
      IMAGE_VERIFICATION_CONTRACT_VERSION \
      IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS \
      IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS \
      IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS \
      IMAGE_VERIFICATION_MODEL_TIMEOUT_SECONDS \
      IMAGE_VERIFICATION_REASONING_RETRY_MIN_BUDGET_SECONDS \
      IMAGE_VERIFICATION_MODEL_CONCURRENCY \
      IMAGE_VERIFICATION_OCR_BACKEND \
      DISPATCHER_MEMORY_LIMIT \
      DISPATCHER_STOP_GRACE_PERIOD \
      DISPATCHER_RECYCLE_ENABLED \
      DISPATCHER_RECYCLE_SOFT_RSS_BYTES \
      DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES \
      DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT \
      DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS \
      DISPATCHER_RECYCLE_LEASE_SECONDS \
      DISPATCHER_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS
    if ! is_true "${DISPATCHER_RECYCLE_ENABLED}"; then
      echo "DISPATCHER_RECYCLE_ENABLED must be true when verification contract is enabled." >&2
      exit 1
    fi
    validate_verification_contract_values
  fi

  if [[ "${IMAGE_VERIFICATION_OCR_BACKEND:-local}" == "remote" ]] \
    && ! is_true "${IMAGE_VERIFICATION_CONTRACT_ENABLED:-false}"; then
    echo "Remote OCR requires IMAGE_VERIFICATION_CONTRACT_ENABLED=true." >&2
    exit 1
  fi

  if verification_remote_enabled; then
    require_runtime_values \
      TGYUNYING_IMAGE_VERIFICATION_IMAGE \
      IMAGE_VERIFICATION_WORKER_TOKEN \
      IMAGE_VERIFICATION_MAX_IMAGE_BYTES \
      IMAGE_VERIFICATION_MAX_IMAGE_PIXELS \
      IMAGE_VERIFICATION_MAX_IMAGE_DIMENSION \
      IMAGE_VERIFICATION_WORKER_MAX_BUDGET_SECONDS \
      IMAGE_VERIFICATION_RECOVERY_OBSERVATION_SECONDS \
      IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS \
      IMAGE_VERIFICATION_WORKER_RECYCLE_REQUEST_LIMIT \
      IMAGE_VERIFICATION_WORKER_SOFT_RSS_BYTES \
      IMAGE_VERIFICATION_WORKER_MEMORY_LIMIT \
      IMAGE_VERIFICATION_WORKER_STOP_GRACE_PERIOD
    validate_remote_verification_values
  fi
}

compose() {
  local compose_files=(-f "$COMPOSE_FILE")
  if is_true "${IMAGE_VERIFICATION_CONTRACT_ENABLED:-false}"; then
    compose_files+=(-f "${APP_DIR}/docker-compose.dispatcher-runtime.yml")
  fi
  if verification_remote_enabled; then
    compose_files+=(-f "${APP_DIR}/docker-compose.image-verification.yml")
  fi
  (cd "$APP_DIR" && docker compose "${compose_files[@]}" --env-file "$ENV_FILE" "$@")
}

is_true() {
  case "$1" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    *) return 1 ;;
  esac
}

verification_remote_enabled() {
  is_true "${IMAGE_VERIFICATION_CONTRACT_ENABLED:-false}" \
    && [[ "${IMAGE_VERIFICATION_OCR_BACKEND:-local}" == "remote" ]]
}

require_runtime_values() {
  local missing=()
  local key
  for key in "$@"; do
    if [[ -z "${!key:-}" ]]; then
      missing+=("$key")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    echo "Missing verification runtime env vars: ${missing[*]}" >&2
    exit 1
  fi
}

validate_verification_contract_values() {
  require_positive_number IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS
  require_positive_number IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS
  require_positive_number IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS
  require_positive_number IMAGE_VERIFICATION_MODEL_TIMEOUT_SECONDS
  require_positive_number IMAGE_VERIFICATION_REASONING_RETRY_MIN_BUDGET_SECONDS
  require_positive_integer IMAGE_VERIFICATION_MODEL_CONCURRENCY
  require_positive_integer DISPATCHER_RECYCLE_LEASE_SECONDS
  require_positive_number DISPATCHER_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS
  require_nonnegative_number DISPATCHER_RECYCLE_SOFT_RSS_BYTES
  require_nonnegative_number DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES
  require_nonnegative_number DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT
  require_nonnegative_number DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS
  require_memory_value DISPATCHER_MEMORY_LIMIT
  require_duration_value DISPATCHER_STOP_GRACE_PERIOD
  require_verification_deadline_relationships
  require_dispatcher_recycle_threshold
  if [[ "$IMAGE_VERIFICATION_OCR_BACKEND" != "remote" ]]; then
    echo "Production verification contract requires IMAGE_VERIFICATION_OCR_BACKEND=remote." >&2
    return 1
  fi
}

validate_remote_verification_values() {
  require_positive_integer IMAGE_VERIFICATION_MAX_IMAGE_BYTES
  require_positive_integer IMAGE_VERIFICATION_MAX_IMAGE_PIXELS
  require_positive_integer IMAGE_VERIFICATION_MAX_IMAGE_DIMENSION
  require_positive_number IMAGE_VERIFICATION_WORKER_MAX_BUDGET_SECONDS
  require_positive_number IMAGE_VERIFICATION_RECOVERY_OBSERVATION_SECONDS
  require_positive_number IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS
  require_positive_integer IMAGE_VERIFICATION_WORKER_RECYCLE_REQUEST_LIMIT
  require_positive_integer IMAGE_VERIFICATION_WORKER_SOFT_RSS_BYTES
  require_memory_value IMAGE_VERIFICATION_WORKER_MEMORY_LIMIT
  require_duration_value IMAGE_VERIFICATION_WORKER_STOP_GRACE_PERIOD
  if ! awk \
    -v ttl="$IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS" \
    -v budget="$IMAGE_VERIFICATION_WORKER_MAX_BUDGET_SECONDS" \
    -v recovery="$IMAGE_VERIFICATION_RECOVERY_OBSERVATION_SECONDS" \
    'BEGIN { exit !(ttl >= budget + recovery) }'; then
    echo "IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS must be at least worker budget plus recovery observation." >&2
    return 1
  fi
}

require_verification_deadline_relationships() {
  if ! awk \
    -v acceptance="$IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS" \
    -v headroom="$IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS" \
    -v tail="$IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS" \
    'BEGIN { window = acceptance - headroom; exit !(headroom < acceptance && tail < window) }'; then
    echo "Verification deadline values require headroom < acceptance and model tail < acceptance - headroom." >&2
    return 1
  fi
}

require_dispatcher_recycle_threshold() {
  if ! awk \
    -v rss="$DISPATCHER_RECYCLE_SOFT_RSS_BYTES" \
    -v cgroup="$DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES" \
    -v attempts="$DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT" \
    -v uptime="$DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS" \
    'BEGIN { exit !(rss > 0 || cgroup > 0 || attempts > 0 || uptime > 0) }'; then
    echo "Dispatcher recycle requires at least one positive threshold." >&2
    return 1
  fi
}

require_positive_number() {
  local name="$1"
  local value="${!name:-}"
  if ! printf '%s\n' "$value" | grep -Eq '^([0-9]+([.][0-9]*)?|[.][0-9]+)$' \
    || ! awk -v value="$value" 'BEGIN { exit !(value > 0) }'; then
    echo "$name must be a positive number." >&2
    return 1
  fi
}

require_nonnegative_number() {
  local name="$1"
  local value="${!name:-}"
  if ! printf '%s\n' "$value" | grep -Eq '^([0-9]+([.][0-9]*)?|[.][0-9]+)$'; then
    echo "$name must be a non-negative number." >&2
    return 1
  fi
}

require_positive_integer() {
  local name="$1"
  local value="${!name:-}"
  if ! printf '%s\n' "$value" | grep -Eq '^[1-9][0-9]*$'; then
    echo "$name must be a positive integer." >&2
    return 1
  fi
}

require_memory_value() {
  local name="$1"
  local value="${!name:-}"
  if ! printf '%s\n' "$value" | grep -Eiq '^[1-9][0-9]*([bkmg]|[kmg]b)?$'; then
    echo "$name must be a positive Docker memory value such as 512m or 1g." >&2
    return 1
  fi
}

require_duration_value() {
  local name="$1"
  local value="${!name:-}"
  if ! printf '%s\n' "$value" | grep -Eq '^[1-9][0-9]*(ms|s|m|h)$'; then
    echo "$name must be a positive duration such as 90s or 2m." >&2
    return 1
  fi
}
