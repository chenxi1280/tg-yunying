from __future__ import annotations

import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.grok_cli_bridge import GrokCliBridge, GrokCliUnavailable


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path, **overrides):
    values = {
        "grok_cli_enabled": True,
        "grok_cli_bin": "/root/.grok/bin/grok",
        "grok_cli_model": "grok-4.5",
        "grok_cli_timeout_seconds": 45,
        "grok_cli_lock_path": str(tmp_path / "grok.lock"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_grok_cli_bridge_runs_bounded_command_and_parses_drafts(tmp_path):
    captured = {}

    def runner(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        envelope = {"text": json.dumps({"drafts": [{"content": "老师今天高跟鞋挺好看"}]}), "stopReason": "EndTurn"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    result = GrokCliBridge(_settings(tmp_path), runner=runner).generate(
        system_prompt="system",
        user_prompt="user",
        count=1,
    )

    assert [item.content for item in result.candidates] == ["老师今天高跟鞋挺好看"]
    assert captured["args"][0] == "/root/.grok/bin/grok"
    assert "--no-memory" in captured["args"]
    assert "--no-subagents" in captured["args"]
    assert "--disable-web-search" in captured["args"]
    assert captured["kwargs"]["timeout"] == 45
    assert captured["kwargs"]["shell"] is False


def test_grok_cli_bridge_rejects_disabled_nonzero_and_non_end_turn(tmp_path):
    with pytest.raises(GrokCliUnavailable, match="disabled"):
        GrokCliBridge(_settings(tmp_path, grok_cli_enabled=False)).generate(system_prompt="s", user_prompt="u", count=1)

    def nonzero(*_args, **_kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="auth required")

    with pytest.raises(GrokCliUnavailable, match="exit_2"):
        GrokCliBridge(_settings(tmp_path), runner=nonzero).generate(system_prompt="s", user_prompt="u", count=1)

    def interrupted(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"text": "{}", "stopReason": "ToolUse"}), stderr="")

    with pytest.raises(GrokCliUnavailable, match="stop_reason"):
        GrokCliBridge(_settings(tmp_path), runner=interrupted).generate(system_prompt="s", user_prompt="u", count=1)


def test_grok_cli_bridge_shared_lock_is_explicit(tmp_path):
    settings = _settings(tmp_path)
    lock_file = open(settings.grok_cli_lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(GrokCliUnavailable, match="capacity_busy"):
            GrokCliBridge(settings).generate(system_prompt="s", user_prompt="u", count=1)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_production_image_and_preflight_include_bridge_runtime_dependencies():
    dockerfile = (PROJECT_ROOT / "Dockerfile.backend").read_text()
    workflow = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text()

    assert "curl ca-certificates git" in dockerfile
    assert "docker exec tgyunying-worker-planner git --version" in workflow
