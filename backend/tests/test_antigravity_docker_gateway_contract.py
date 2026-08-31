from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _gateway_result(
    tmp_path: Path, gateway: str, *, docker_exit: int = 0,
) -> tuple[subprocess.CompletedProcess, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >"${FAKE_DOCKER_LOG}"\n'
        'if [[ "${FAKE_DOCKER_EXIT}" != "0" ]]; then exit "${FAKE_DOCKER_EXIT}"; fi\n'
        'printf \'%s\\n\' "${FAKE_GATEWAY}"\n'
    )
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_DOCKER_EXIT": str(docker_exit),
        "FAKE_GATEWAY": gateway,
        "INFRA_NETWORK_NAME": "custom_infra",
    }
    result = subprocess.run(
        [
            "bash", "-c",
            "source deploy/docker-env.sh; "
            "initialize_antigravity_docker_gateway_env; "
            "printf '%s' \"${ANTIGRAVITY_DOCKER_GATEWAY}\"",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log_path


def test_compose_maps_only_generation_consumers_to_resolved_gateway() -> None:
    compose = (PROJECT_ROOT / "docker-compose.server.yml").read_text()
    mapping = (
        "host.docker.internal:"
        "${ANTIGRAVITY_DOCKER_GATEWAY:?ANTIGRAVITY_DOCKER_GATEWAY is required}"
    )
    assert compose.count(mapping) == 4
    assert "host.docker.internal:host-gateway" not in compose


def test_gateway_resolver_uses_named_infra_network(tmp_path: Path) -> None:
    result, log_path = _gateway_result(tmp_path, "172.19.0.1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "172.19.0.1"
    assert log_path.read_text().startswith("network inspect custom_infra --format")


def test_gateway_resolver_rejects_missing_network(tmp_path: Path) -> None:
    result, _log_path = _gateway_result(tmp_path, "", docker_exit=1)
    assert result.returncode != 0
    assert "Unable to resolve custom_infra gateway" in result.stderr


@pytest.mark.parametrize("gateway", ("", "172.19.0.999", "not-an-ip"))
def test_gateway_resolver_rejects_invalid_ipv4(tmp_path: Path, gateway: str) -> None:
    result, _log_path = _gateway_result(tmp_path, gateway)
    assert result.returncode != 0
    assert "Invalid custom_infra IPv4 gateway" in result.stderr


def test_slot_lifecycle_uses_same_named_network() -> None:
    for relative_path in (
        "deploy/install-antigravity-provider-slot.sh",
        "deploy/restart-antigravity-provider-slots.sh",
    ):
        source = (PROJECT_ROOT / relative_path).read_text()
        assert 'INFRA_NETWORK_NAME="${INFRA_NETWORK_NAME:-infra_default}"' in source
        assert 'docker network inspect "${INFRA_NETWORK_NAME}"' in source
