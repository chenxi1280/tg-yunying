#!/usr/bin/env bash
# Loaded by compose-up.sh after its runtime environment and Compose function.

stop_all_release_workers() {
  local inventory worker_ids stopped_state started_at
  local ids=()
  started_at="$(date +%s)"
  inventory="$(compose ps --all --format json)"
  worker_ids="$(printf '%s' "$inventory" | python3 "$SCRIPT_DIR/worker-inventory.py")"
  if [[ -z "$worker_ids" ]]; then
    echo "WORKER_STOP_COUNT=0 elapsed_seconds=$(( $(date +%s) - started_at ))"
    return
  fi
  while read -r container_id; do ids+=("$container_id"); done <<< "$worker_ids"
  # Docker stops the frozen container IDs together, including workers disabled
  # in the new configuration. No Compose dependency waits between worker roles.
  docker stop "${ids[@]}"
  stopped_state="$(docker inspect --format '{{.Id}} {{.State.Status}}' "${ids[@]}")"
  while read -r container_id status; do
    case "$status" in
      exited|created|dead) ;;
      *) echo "Release worker remains active: $container_id status=$status" >&2; return 1 ;;
    esac
  done <<< "$stopped_state"
  echo "WORKER_STOP_COUNT=${#ids[@]} elapsed_seconds=$(( $(date +%s) - started_at ))"
}
