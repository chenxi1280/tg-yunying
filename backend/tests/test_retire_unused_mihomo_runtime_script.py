from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/retire_unused_mihomo_runtime.py"
)
pytestmark = pytest.mark.no_postgres


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "retire_unused_mihomo_runtime", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _preview(script, *, consumers: int = 0) -> dict[str, object]:
    payload = {
        "name": "tgyunying-mihomo-018",
        "consumers": {"accounts": consumers},
    }
    runtime = {
        "name": payload["name"],
        "container_id": "container-id",
        "image_id": "image-id",
        "running": True,
        "restart_policy": "unless-stopped",
        "config_source": "/config.yaml",
        "config_sha256": "config-hash",
    }
    manifest = {
        "version": 1,
        "deployed_release": "/data/releases/build_deadbeef",
        "targets": [
            {
                "name": payload["name"],
                "db": {"payload": payload, "state_hash": "db-hash"},
                "runtime": runtime,
            }
        ],
        "non_target_runtime_manifest_hash": "other-hash",
    }
    return {**manifest, "manifest_hash": script.manifest_hash(manifest)}


def test_targets_are_exact_and_deduplicated() -> None:
    script = _load_script()

    assert script.validate_targets(
        ["tgyunying-mihomo-018", "tgyunying-mihomo-018"]
    ) == ("tgyunying-mihomo-018",)
    with pytest.raises(RuntimeError, match="proxy_runtime_target_invalid"):
        script.validate_targets(["tgyunying-mihomo-all"])


def test_apply_requires_release_manifest_actor_and_approval() -> None:
    script = _load_script()
    args = script.parse_args(["--target", "tgyunying-mihomo-018", "--apply"])

    with pytest.raises(RuntimeError, match="proxy_runtime_apply_input_required"):
        script.validate_apply_inputs(args, _preview(script))


def test_any_consumer_blocks_apply() -> None:
    script = _load_script()

    with pytest.raises(RuntimeError, match="proxy_runtime_has_consumers"):
        script.validate_zero_consumers(_preview(script, consumers=1))


def test_stop_fences_restart_policy_before_stop(monkeypatch) -> None:
    script = _load_script()
    record = _preview(script)["targets"][0]
    commands: list[list[str]] = []
    monkeypatch.setattr(script, "inspect_runtime", lambda _name: record["runtime"])

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(script, "run_command", fake_run)

    script.stop_runtime(record)

    assert commands == [
        ["docker", "update", "--restart=no", "tgyunying-mihomo-018"],
        ["docker", "stop", "--time", "30", "tgyunying-mihomo-018"],
    ]
