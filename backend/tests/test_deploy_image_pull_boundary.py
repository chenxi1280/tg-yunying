from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
COMPOSE_SCRIPT = Path(__file__).resolve().parents[2] / "deploy/compose-up.sh"
REGISTRY_FAILURE = 42
DOCKER_STUB = """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$DOCKER_CALLS_FILE"
if [[ "$1 $2" == "network inspect" ]]; then
  printf '172.25.0.1\\n'
  exit 0
fi
if [[ "$1" == "compose" && " $* " == *" pull backend "* ]]; then
  [[ "$FAILED_IMAGE" == "backend" ]] || exit 0
elif [[ "$1" == "pull" && "$2" == "registry.invalid/frontend:test" ]]; then
  [[ "$FAILED_IMAGE" == "frontend" ]] || exit 0
else
  printf 'unexpected Docker operation\\n' >&2
  exit 98
fi
printf 'registry TLS handshake timeout\\n' >&2
exit 42
"""


def _runtime_env(tmp_path: Path, failed_image: str) -> dict:
    docker = tmp_path / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    runtime_env = tmp_path / ".env"
    runtime_env.write_text("\n".join([
        "TGYUNYING_BACKEND_IMAGE=registry.invalid/backend:test",
        "TGYUNYING_FRONTEND_IMAGE=registry.invalid/frontend:test",
        "DATABASE_URL=postgresql://unreachable/tg_yunying_test",
        "REDIS_URL=redis://unreachable/0",
        "SESSION_SECRET_KEY=isolated-test-placeholder",
        "ADMIN_BOOTSTRAP_PASSWORD=isolated-test-placeholder",
        "CORS_ORIGINS=http://example.invalid",
        "PUBLIC_APP_BASE_URL=http://example.invalid",
        "IMAGE_VERIFICATION_CONTRACT_ENABLED=false",
        "ENABLE_EMBEDDED_WORKER=false",
    ]))
    return {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "APP_DIR": str(tmp_path), "BASE_DIR": str(tmp_path),
        "ENV_FILE": str(runtime_env),
        "IMAGE_ENV_FILE": str(tmp_path / "missing-image-env"),
        "DOCKER_CALLS_FILE": str(tmp_path / "docker-calls"),
        "FAILED_IMAGE": failed_image,
    }


@pytest.mark.parametrize("failed_image", ["backend", "frontend"])
def test_image_pull_failure_exits_before_cleanup_or_worker_fence(tmp_path, failed_image):
    result = subprocess.run(
        ["bash", str(COMPOSE_SCRIPT)], env=_runtime_env(tmp_path, failed_image),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == REGISTRY_FAILURE, result.stderr
    assert "registry TLS handshake timeout" in result.stderr
    calls = (tmp_path / "docker-calls").read_text().splitlines()
    assert calls[0].startswith("network inspect ")
    assert calls[1].endswith("pull backend")
    expected_count = 2 if failed_image == "backend" else 3
    assert len(calls) == expected_count
    if failed_image == "frontend":
        assert calls[-1] == "pull registry.invalid/frontend:test"
