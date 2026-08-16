from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _valid_contract_env() -> dict[str, str]:
    return {
        "IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS": "20",
        "IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS": "2",
        "IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS": "5",
        "IMAGE_VERIFICATION_MODEL_TIMEOUT_SECONDS": "4",
        "IMAGE_VERIFICATION_REASONING_RETRY_MIN_BUDGET_SECONDS": "1",
        "IMAGE_VERIFICATION_MODEL_CONCURRENCY": "1",
        "IMAGE_VERIFICATION_OCR_BACKEND": "remote",
        "SEARCH_DISPATCHER_CONCURRENCY": "2",
        "DISPATCHER_RECYCLE_LEASE_SECONDS": "60",
        "DISPATCHER_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS": "15",
        "DISPATCHER_RECYCLE_SOFT_RSS_BYTES": "536870912",
        "DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES": "0",
        "DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT": "0",
        "DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS": "0",
        "DISPATCHER_MEMORY_LIMIT": "768m",
        "DISPATCHER_STOP_GRACE_PERIOD": "90s",
    }


def _valid_remote_env() -> dict[str, str]:
    return {
        "IMAGE_VERIFICATION_MAX_IMAGE_BYTES": "1000000",
        "IMAGE_VERIFICATION_MAX_IMAGE_PIXELS": "4000000",
        "IMAGE_VERIFICATION_MAX_IMAGE_DIMENSION": "4096",
        "IMAGE_VERIFICATION_WORKER_MAX_BUDGET_SECONDS": "12",
        "IMAGE_VERIFICATION_RECOVERY_OBSERVATION_SECONDS": "8",
        "IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS": "20",
        "IMAGE_VERIFICATION_WORKER_RECYCLE_REQUEST_LIMIT": "100",
        "IMAGE_VERIFICATION_WORKER_SOFT_RSS_BYTES": "268435456",
        "IMAGE_VERIFICATION_WORKER_MEMORY_LIMIT": "384m",
        "IMAGE_VERIFICATION_WORKER_STOP_GRACE_PERIOD": "30s",
    }


def _run_validator(function: str, values: dict[str, str]) -> subprocess.CompletedProcess:
    environment = {**os.environ, **values}
    return subprocess.run(
        [
            "bash",
            "-c",
            f"source deploy/docker-env.sh; {function}",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_contract_release_values_accept_valid_calibration() -> None:
    result = _run_validator(
        "validate_verification_contract_values",
        _valid_contract_env(),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS", "0"),
        ("IMAGE_VERIFICATION_MODEL_CONCURRENCY", "1.5"),
        ("SEARCH_DISPATCHER_CONCURRENCY", "0"),
        ("DISPATCHER_MEMORY_LIMIT", "large"),
        ("DISPATCHER_STOP_GRACE_PERIOD", "90"),
    ),
)
def test_contract_release_values_reject_invalid_types(name, value) -> None:
    values = {**_valid_contract_env(), name: value}

    assert _run_validator(
        "validate_verification_contract_values",
        values,
    ).returncode != 0


def test_contract_release_values_reject_impossible_deadline_window() -> None:
    values = {
        **_valid_contract_env(),
        "IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS": "8",
        "IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS": "13",
    }

    result = _run_validator("validate_verification_contract_values", values)

    assert result.returncode != 0
    assert "model tail" in result.stderr


def test_contract_release_values_reject_local_ocr_backend() -> None:
    values = {
        **_valid_contract_env(),
        "IMAGE_VERIFICATION_OCR_BACKEND": "local",
    }

    result = _run_validator("validate_verification_contract_values", values)

    assert result.returncode != 0
    assert "requires IMAGE_VERIFICATION_OCR_BACKEND=remote" in result.stderr


def test_remote_release_values_require_sufficient_terminal_ttl() -> None:
    invalid = {
        **_valid_remote_env(),
        "IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS": "19.9",
    }

    assert _run_validator(
        "validate_remote_verification_values",
        _valid_remote_env(),
    ).returncode == 0
    result = _run_validator("validate_remote_verification_values", invalid)
    assert result.returncode != 0
    assert "at least worker budget" in result.stderr


def test_actions_release_persists_verification_runtime_contract() -> None:
    release = (PROJECT_ROOT / "deploy/release.sh").read_text()
    workflow = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text()
    required_names = {
        "IMAGE_VERIFICATION_CONTRACT_ENABLED",
        "IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS",
        "IMAGE_VERIFICATION_OCR_BACKEND",
        "SEARCH_DISPATCHER_CONCURRENCY",
        "IMAGE_VERIFICATION_WORKER_TOKEN",
        "DISPATCHER_MEMORY_LIMIT",
        "DISPATCHER_STOP_GRACE_PERIOD",
        "IMAGE_VERIFICATION_WORKER_MEMORY_LIMIT",
    }

    assert '>>"$image_env_path"' in release
    for name in required_names:
        assert name in release
        assert f"{name}:" in workflow
    assert "secrets.TGYUNYING_IMAGE_VERIFICATION_WORKER_TOKEN" in workflow


def test_image_worker_uses_functional_readiness_app_entrypoint() -> None:
    compose = (PROJECT_ROOT / "docker-compose.image-verification.yml").read_text()
    dockerfile = (PROJECT_ROOT / "Dockerfile.image-verification-worker").read_text()

    assert "app.image_verification_worker_app:app" in compose
    assert "app.image_verification_worker_app:app" in dockerfile
    assert "/internal/v1/image-verification/ready" in compose
    assert "IMAGE_VERIFICATION_WORKER_TOKEN" in compose
