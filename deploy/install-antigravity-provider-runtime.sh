#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${SOURCE_DIR:?SOURCE_DIR is required}"
RELEASE_SHA="${RELEASE_SHA:?RELEASE_SHA is required}"
RUNTIME_ROOT="${ANTIGRAVITY_RUNTIME_ROOT:-/usr/local/lib/tgyunying-antigravity}"
RUNTIME_RELEASE="${RUNTIME_ROOT}/releases/${RELEASE_SHA}"
RUNTIME_CURRENT="${RUNTIME_ROOT}/current"
PYTHON_BIN="${ANTIGRAVITY_RUNTIME_PYTHON_BIN:-/usr/bin/python3.11}"
EXPECTED_DIRECTORY_MODE="755"
EXPECTED_FILE_MODE="644"
RUNTIME_FILES=(
  antigravity_provider_server.py
  antigravity_provider_ledger.py
  antigravity_provider_protocol.py
  antigravity_provider_schema.py
)

if [[ ! "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "full lowercase RELEASE_SHA is required" >&2
  exit 1
fi

install -d -o root -g root -m 0755 "${RUNTIME_ROOT}" "${RUNTIME_ROOT}/releases"
if [[ -L "${RUNTIME_RELEASE}" ]]; then
  echo "ANTIGRAVITY_RUNTIME_RELEASE_INVALID=${RUNTIME_RELEASE}" >&2
  exit 1
elif [[ -d "${RUNTIME_RELEASE}" ]]; then
  for runtime_file in "${RUNTIME_FILES[@]}"; do
    cmp -s "${SOURCE_DIR}/${runtime_file}" "${RUNTIME_RELEASE}/${runtime_file}" || {
      echo "Antigravity runtime SHA content drift: ${runtime_file}" >&2
      exit 1
    }
  done
elif [[ -e "${RUNTIME_RELEASE}" ]]; then
  echo "ANTIGRAVITY_RUNTIME_RELEASE_INVALID=${RUNTIME_RELEASE}" >&2
  exit 1
else
  stage_dir="$(mktemp -d "${RUNTIME_ROOT}/.stage-${RELEASE_SHA}.XXXXXX")"
  trap 'rm -rf -- "${stage_dir}"' EXIT
  for runtime_file in "${RUNTIME_FILES[@]}"; do
    install -o root -g root -m 0644 \
      "${SOURCE_DIR}/${runtime_file}" "${stage_dir}/${runtime_file}"
  done
  chmod 0755 "${stage_dir}"
  mv "${stage_dir}" "${RUNTIME_RELEASE}"
  trap - EXIT
fi

if [[ -L "${RUNTIME_RELEASE}" || ! -d "${RUNTIME_RELEASE}" ]]; then
  echo "ANTIGRAVITY_RUNTIME_RELEASE_INVALID=${RUNTIME_RELEASE}" >&2
  exit 1
fi
if [[ "$(stat -c '%U:%G' "${RUNTIME_RELEASE}")" != "root:root" ]]; then
  echo "ANTIGRAVITY_RUNTIME_OWNER_DRIFT=${RUNTIME_RELEASE}" >&2
  exit 1
fi
if [[ "$(stat -c '%a' "${RUNTIME_RELEASE}")" != "${EXPECTED_DIRECTORY_MODE}" ]]; then
  echo "ANTIGRAVITY_RUNTIME_MODE_DRIFT=${RUNTIME_RELEASE}" >&2
  exit 1
fi
for runtime_file in "${RUNTIME_FILES[@]}"; do
  runtime_path="${RUNTIME_RELEASE}/${runtime_file}"
  if [[ ! -f "${runtime_path}" || -L "${runtime_path}" ]]; then
    echo "ANTIGRAVITY_RUNTIME_FILE_INVALID=${runtime_path}" >&2
    exit 1
  fi
  if [[ "$(stat -c '%U:%G' "${runtime_path}")" != "root:root" ]]; then
    echo "ANTIGRAVITY_RUNTIME_OWNER_DRIFT=${runtime_path}" >&2
    exit 1
  fi
  if [[ "$(stat -c '%a' "${runtime_path}")" != "${EXPECTED_FILE_MODE}" ]]; then
    echo "ANTIGRAVITY_RUNTIME_MODE_DRIFT=${runtime_path}" >&2
    exit 1
  fi
done

if [[ -e "${RUNTIME_CURRENT}" && ! -L "${RUNTIME_CURRENT}" ]]; then
  echo "ANTIGRAVITY_RUNTIME_CURRENT_INVALID=${RUNTIME_CURRENT}" >&2
  exit 1
fi
ln -sfn "${RUNTIME_RELEASE}" "${RUNTIME_CURRENT}.tmp"
"${PYTHON_BIN}" -E -s -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
  "${RUNTIME_CURRENT}.tmp" "${RUNTIME_CURRENT}"
echo "ANTIGRAVITY_RUNTIME=${RUNTIME_RELEASE}"
