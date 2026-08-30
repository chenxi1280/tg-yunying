from __future__ import annotations

import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.antigravity_cli_bridge import (
    AntigravityCliBridge,
    AntigravityCliUnavailable,
    clean_markdown_json_fences,
)


pytestmark = pytest.mark.no_postgres


def _settings(tmp_path: Path, **overrides):
    values = {
        "antigravity_cli_enabled": True,
        "antigravity_cli_bin": "/root/.local/bin/agy",
        "antigravity_cli_model": "gemini-3.5-flash",
        "antigravity_cli_effort": "medium",
        "antigravity_cli_timeout_seconds": 45,
        "antigravity_cli_lock_path": str(tmp_path / "antigravity.lock"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_clean_markdown_json_fences():
    raw_md = "```json\n{\"drafts\": [{\"content\": \"走西边水库那条线呗\"}]}\n```"
    assert clean_markdown_json_fences(raw_md) == '{"drafts": [{"content": "走西边水库那条线呗"}]}'

    raw_plain = '{"drafts": [{"content": "测试普通json"}]}'
    assert clean_markdown_json_fences(raw_plain) == raw_plain


def test_antigravity_cli_bridge_runs_bounded_command_and_parses_drafts(tmp_path):
    captured = {}

    def runner(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        output_json = "```json\n" + json.dumps({
            "drafts": [
                {"slot_id": "s1", "persona": "自驾老手", "content": "走西边水库那条线呗，路宽车少好开", "risk_level": "低"}
            ]
        }) + "\n```"
        return SimpleNamespace(returncode=0, stdout=output_json, stderr="")

    result = AntigravityCliBridge(_settings(tmp_path), runner=runner).generate(
        system_prompt="system",
        user_prompt="user",
        count=1,
    )

    assert [item.content for item in result.candidates] == ["走西边水库那条线呗，路宽车少好开"]
    assert captured["args"][0] == "/root/.local/bin/agy"
    assert "--model" in captured["args"]
    assert "gemini-3.5-flash" in captured["args"]
    assert "--effort" in captured["args"]
    assert "medium" in captured["args"]
    assert captured["kwargs"]["timeout"] == 45
    assert captured["kwargs"]["shell"] is False


def test_antigravity_cli_bridge_rejects_disabled_and_nonzero(tmp_path):
    with pytest.raises(AntigravityCliUnavailable, match="disabled"):
        AntigravityCliBridge(_settings(tmp_path, antigravity_cli_enabled=False)).generate(
            system_prompt="s", user_prompt="u", count=1
        )

    def nonzero(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="authentication required")

    with pytest.raises(AntigravityCliUnavailable, match="exit_1"):
        AntigravityCliBridge(_settings(tmp_path), runner=nonzero).generate(
            system_prompt="s", user_prompt="u", count=1
        )


def test_antigravity_cli_bridge_shared_lock_is_explicit(tmp_path):
    settings = _settings(tmp_path)
    lock_file = open(settings.antigravity_cli_lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(AntigravityCliUnavailable, match="capacity_busy"):
            AntigravityCliBridge(settings).generate(system_prompt="s", user_prompt="u", count=1)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
