#!/usr/bin/env bash

set -euo pipefail

RELEASE_SSH_ATTEMPTS="${RELEASE_SSH_ATTEMPTS:-3}"
RELEASE_SSH_RETRY_DELAY="${RELEASE_SSH_RETRY_DELAY:-10}"

if [[ $# -lt 2 ]]; then
  echo "Usage: bash deploy/ssh_retry.sh <host> <remote-command>" >&2
  exit 2
fi

if [[ ! "$RELEASE_SSH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  echo "RELEASE_SSH_ATTEMPTS must be a positive integer" >&2
  exit 2
fi
if [[ ! "$RELEASE_SSH_RETRY_DELAY" =~ ^[0-9]+$ ]]; then
  echo "RELEASE_SSH_RETRY_DELAY must be a non-negative integer" >&2
  exit 2
fi

host="$1"
shift

for ((attempt = 1; attempt <= RELEASE_SSH_ATTEMPTS; attempt++)); do
  echo "==> SSH ${host} (attempt ${attempt}/${RELEASE_SSH_ATTEMPTS})"
  if ssh "$host" "$@"; then
    exit 0
  else
    status=$?
  fi
  if ((attempt == RELEASE_SSH_ATTEMPTS)); then
    echo "SSH ${host} failed after ${RELEASE_SSH_ATTEMPTS} attempt(s)" >&2
    exit "$status"
  fi
  echo "SSH ${host} failed with exit code ${status}; retrying in ${RELEASE_SSH_RETRY_DELAY}s" >&2
  sleep "$RELEASE_SSH_RETRY_DELAY"
done
