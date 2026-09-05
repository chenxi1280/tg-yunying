from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from tests.test_antigravity_deploy_contract import (
    PROJECT_ROOT, RUNTIME_FILES, _executable, _restart_env,
)


pytestmark = pytest.mark.no_postgres


def _preservation_env(tmp_path: Path, *, active=True):
    env, log = _restart_env(tmp_path, enabled=True, active=active)
    current = Path(env["ANTIGRAVITY_RUNTIME_ROOT"]) / "old"
    candidate = Path(env["RELEASE_DIR"]) / "backend/scripts"
    for name in RUNTIME_FILES:
        (current / name).write_text("identical source:" + name)
        (candidate / name).write_text((current / name).read_text())
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    _executable(fake_bin / "stat", """#!/usr/bin/env bash
if [[ "$2" == '%U:%G' ]]; then echo root:root
elif [[ -d "$3" ]]; then echo 755
else echo 644
fi
""")
    _executable(fake_bin / "readlink", """#!/usr/bin/env python3
import os,sys
print(os.path.realpath(sys.argv[-1]) if '-f' in sys.argv else os.readlink(sys.argv[-1]))
""")
    _executable(Path(env["ANTIGRAVITY_TIMEOUT_BIN"]), """#!/usr/bin/env bash
printf 'probe-mode:%s\n' "${@: -1}" >>"${FAKE_SLOT_LOG}"
exit "${FAKE_PROBE_EXIT:-0}"
""")
    return env, log, current, candidate


def _run(env):
    return subprocess.run(["bash", str(PROJECT_ROOT / "deploy/restart-antigravity-provider-slots.sh")],
        env=env, capture_output=True, text=True, timeout=10)


def test_identical_active_runtime_is_observed_without_install_or_restart(tmp_path):
    env, log, current, _candidate = _preservation_env(tmp_path)
    result = _run(env)

    assert result.returncode == 0
    assert "preserved_unchanged" in result.stdout
    assert log.read_text().splitlines() == ["probe-mode:--observe-runtime"]
    assert (Path(env["ANTIGRAVITY_RUNTIME_ROOT"]) / "current").resolve() == current


def test_observation_failure_preserves_process_and_does_not_start_install(tmp_path):
    env, log, current, _candidate = _preservation_env(tmp_path)
    result = _run({**env, "FAKE_PROBE_EXIT": "7"})

    assert result.returncode == 7
    assert "preserved_unchanged" not in result.stdout
    assert log.read_text().splitlines() == ["probe-mode:--observe-runtime"]
    assert (Path(env["ANTIGRAVITY_RUNTIME_ROOT"]) / "current").resolve() == current


@pytest.mark.parametrize("change", ["source", "inactive"])
def test_changed_or_inactive_runtime_keeps_full_install_and_model_probe(tmp_path, change):
    env, log, _current, candidate = _preservation_env(tmp_path, active=change != "inactive")
    if change == "source":
        (candidate / RUNTIME_FILES[0]).write_text("new runtime")
    result = _run(env)

    assert result.returncode == 0
    assert "runtime-installed" in log.read_text()
    assert "restart:tgyunying-antigravity-slot-01.service" in log.read_text()
    assert "--observe-runtime" not in log.read_text()
    assert "preserved_unchanged" not in result.stdout


def _checker(monkeypatch, health):
    path = PROJECT_ROOT / "deploy/check-antigravity-provider-slot.py"
    spec = importlib.util.spec_from_file_location("slot_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {"ANTIGRAVITY_BRIDGE_URL": "http://bridge.invalid",
        "ANTIGRAVITY_BRIDGE_TOKEN": "test-token", "ANTIGRAVITY_SLOT_ID": "slot-01",
        "RELEASE_SHA": "a" * 40}.items():
        monkeypatch.setenv(key, value)
    calls = []

    def request(_url, _token, path, payload=None):
        calls.append((path, payload))
        return dict(health)

    monkeypatch.setattr(module, "_request", request)
    return module, calls


def _degraded_health():
    return {"status": "degraded", "bridge_version": "2", "slot_id": "slot-01",
        "binary_ready": True, "cli_version": "1.1.22", "quota_limited": True,
        "confirmed_models": [], "last_terminal_code": "antigravity_quota_limited"}


def test_observation_keeps_quota_failure_visible_and_never_generates(monkeypatch, capsys):
    health = _degraded_health()
    checker, calls = _checker(monkeypatch, health)
    checker.main(["--observe-runtime"])

    result = json.loads(capsys.readouterr().out)
    assert result["provider_health"] == health
    assert result["model_probe_performed"] is False
    assert result["deployment_action"] == "preserved_unchanged"
    assert calls == [("/internal/v1/health", None)]


@pytest.mark.parametrize("field,value", [
    ("binary_ready", False), ("cli_version", "missing"), ("slot_id", "wrong-slot"),
    ("bridge_version", "1"), ("status", "unexpected"),
])
def test_existing_runtime_observation_rejects_unusable_runtime(monkeypatch, field, value):
    checker, calls = _checker(monkeypatch, {**_degraded_health(), field: value})
    with pytest.raises(RuntimeError, match="antigravity_existing_runtime_invalid"):
        checker.main(["--observe-runtime"])
    assert calls == [("/internal/v1/health", None)]
