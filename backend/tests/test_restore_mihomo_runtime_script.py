from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/restore_mihomo_runtime.py"
pytestmark = pytest.mark.no_postgres


def _load_script():
    spec = importlib.util.spec_from_file_location("restore_mihomo_runtime", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_collect_config_sources_accepts_identical_supplemental_copy(tmp_path: Path) -> None:
    script = _load_script()
    primary = tmp_path / "primary"
    supplemental = tmp_path / "supplemental"
    primary.mkdir()
    supplemental.mkdir()
    (primary / "tgyunying-mihomo-001.yaml").write_text("mixed-port: 7890\n")
    (supplemental / "tgyunying-mihomo-001.yaml").write_text("mixed-port: 7890\n")
    (supplemental / "tgyunying-mihomo-002.yaml").write_text("mixed-port: 7891\n")

    sources = script.collect_config_sources(
        str(primary),
        [str(supplemental / "tgyunying-mihomo-001.yaml"), str(supplemental / "tgyunying-mihomo-002.yaml")],
    )

    assert sorted(sources) == ["tgyunying-mihomo-001", "tgyunying-mihomo-002"]
    assert sources["tgyunying-mihomo-001"].path.parent == primary


def test_collect_config_sources_rejects_conflicting_duplicate(tmp_path: Path) -> None:
    script = _load_script()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "tgyunying-mihomo-001.yaml").write_text("mixed-port: 7890\n")
    (second / "tgyunying-mihomo-001.yaml").write_text("mixed-port: 7891\n")

    with pytest.raises(RuntimeError, match="duplicate_config_hash_mismatch"):
        script.collect_config_sources(str(first), [str(second / "tgyunying-mihomo-001.yaml")])


def test_validate_apply_inputs_rejects_missing_proxy_with_consumers() -> None:
    script = _load_script()
    args = script.parse_args([
        "--primary-config-dir", "/tmp/configs",
        "--image", "metacubex/mihomo@sha256:" + "a" * 64,
        "--apply",
        "--expected-deployed-sha", "12345678",
        "--expected-config-manifest-hash", "config",
        "--expected-proxy-manifest-hash", "proxy",
        "--expected-target-manifest-hash", "target",
        "--approval-ref", "INC-1",
        "--allowed-missing-proxy-name", "tgyunying-mihomo-064",
    ])
    preview = {
        "config_manifest_hash": "config",
        "proxy_manifest_hash": "proxy",
        "target_manifest_hash": "target",
        "existing_containers": [],
        "missing_proxy_configs": ["tgyunying-mihomo-064"],
        "proxy_records": [{
            "name": "tgyunying-mihomo-064",
            "direct_accounts": 0,
            "active_slot_bindings": 1,
        }],
        "target_names": ["tgyunying-mihomo-001"],
    }

    with pytest.raises(RuntimeError, match="missing_proxy_has_active_consumers"):
        script.validate_apply_inputs(args, preview)


def test_require_pinned_image_rejects_latest_tag() -> None:
    script = _load_script()

    with pytest.raises(RuntimeError, match="mihomo_image_must_be_pinned_digest"):
        script.require_pinned_image("metacubex/mihomo:latest")


def test_public_preview_omits_proxy_consumer_counts() -> None:
    script = _load_script()

    result = script.public_preview({"proxy_count": 1, "proxy_records": [{"direct_accounts": 1}]})

    assert result == {"proxy_count": 1}


def test_mark_unconfigured_proxies_serializes_health_check_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    captured: dict[str, object] = {}

    def fake_run_command(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(stdout='[{"status":"unhealthy"}]')

    monkeypatch.setattr(script, "run_command", fake_run_command)

    result = script.mark_unconfigured_proxies_unhealthy(
        "backend",
        [{"id": 64, "name": "tgyunying-mihomo-064"}],
        ["tgyunying-mihomo-064"],
        "INC-1",
    )

    assert result == ["tgyunying-mihomo-064"]
    assert "default=str" in captured["command"][-1]
