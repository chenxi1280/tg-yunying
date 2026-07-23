from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SSH_RETRY_SCRIPT = PROJECT_ROOT / "deploy/ssh_retry.sh"
DEPLOY_WORKFLOW = PROJECT_ROOT / ".github/workflows/deploy-production.yml"


def _fake_ssh(tmp_path: Path) -> Path:
    command = tmp_path / "ssh"
    command.write_text(
        """#!/usr/bin/env bash
count="$(cat "${SSH_RETRY_COUNT_FILE}" 2>/dev/null || printf '0')"
count=$((count + 1))
printf '%s' "${count}" > "${SSH_RETRY_COUNT_FILE}"
if (( count < SSH_RETRY_SUCCEEDS_ON )); then
  echo 'Connection timed out during banner exchange' >&2
  exit 255
fi
printf 'connected %s\\n' "$*"
"""
    )
    command.chmod(0o755)
    return command


def _run_retry(tmp_path: Path, succeeds_on: int, attempts: int) -> subprocess.CompletedProcess[str]:
    _fake_ssh(tmp_path)
    count_file = tmp_path / "attempt-count"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RELEASE_SSH_ATTEMPTS": str(attempts),
        "RELEASE_SSH_RETRY_DELAY": "0",
        "SSH_RETRY_COUNT_FILE": str(count_file),
        "SSH_RETRY_SUCCEEDS_ON": str(succeeds_on),
    }
    result = subprocess.run(
        ["bash", str(SSH_RETRY_SCRIPT), "production", "echo ready"],
        text=True,
        capture_output=True,
        env=environment,
        timeout=5,
        check=False,
    )
    result.attempt_count = count_file.read_text()
    return result


def test_ssh_retry_retries_banner_timeout_until_success(tmp_path: Path) -> None:
    result = _run_retry(tmp_path, succeeds_on=3, attempts=3)

    assert result.returncode == 0
    assert result.attempt_count == "3"
    assert "attempt 2/3" in result.stdout
    assert "connected production echo ready" in result.stdout


def test_ssh_retry_surfaces_failure_after_final_attempt(tmp_path: Path) -> None:
    result = _run_retry(tmp_path, succeeds_on=9, attempts=2)

    assert result.returncode == 255
    assert result.attempt_count == "2"
    assert "failed after 2 attempt(s)" in result.stderr


def test_deploy_preflight_and_bridge_check_use_explicit_ssh_retry() -> None:
    workflow = DEPLOY_WORKFLOW.read_text()

    assert workflow.count("bash deploy/ssh_retry.sh silicon-valley-production-server") == 2
