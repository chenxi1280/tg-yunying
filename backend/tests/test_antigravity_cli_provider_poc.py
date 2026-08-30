from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.test_antigravity_cli_provider import (
    POC_SCHEMA,
    PocConfig,
    PocError,
    build_command,
    parse_envelope,
    public_result,
)


pytestmark = pytest.mark.no_postgres


def config() -> PocConfig:
    return PocConfig(Path("/opt/agy"), None, "gemini-test", "low", 30)


def completed(payload: object, *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), stderr)


def test_command_is_bounded_structured_and_does_not_auto_approve_tools():
    command = build_command(config())

    assert command[0] == "/opt/agy"
    assert "--sandbox" in command
    assert "--disable-slash-commands" in command
    assert "--json-schema" in command
    assert json.loads(command[command.index("--json-schema") + 1]) == POC_SCHEMA
    assert "--dangerously-skip-permissions" not in command
    assert "--continue" not in command


def test_success_requires_structured_output_and_returns_sanitized_metadata():
    envelope = parse_envelope(completed({
        "conversation_id": "sensitive-conversation-id",
        "status": "SUCCESS",
        "response": "ignored free text",
        "structured_output": {"status": "ok", "reason": "POC 通过"},
        "duration_seconds": 3.5,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "total_tokens": 12},
    }))

    result = public_result(envelope, config())

    assert result["structured_output"]["reason"] == "POC 通过"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 0
    assert "conversation_id" not in result
    assert "response" not in result


@pytest.mark.parametrize(
    ("payload", "returncode", "stderr", "error_code"),
    [
        ({"status": "SUCCESS"}, 0, "", "antigravity_structured_output_missing"),
        (
            {"status": "SUCCESS", "structured_output": {"status": "wrong", "reason": "x"}},
            0,
            "",
            "antigravity_schema_invalid",
        ),
        (
            {"status": "ERROR", "error": "Eligibility check failed: not eligible"},
            1,
            "",
            "antigravity_account_ineligible",
        ),
        (
            {"status": "ERROR", "error": "authentication required"},
            1,
            "",
            "antigravity_auth_required",
        ),
        (
            {"status": "ERROR", "error": "quota exhausted"},
            1,
            "",
            "antigravity_quota_limited",
        ),
        (
            {"status": "ERROR", "error": "model is not recognized"},
            1,
            "",
            "antigravity_model_invalid",
        ),
    ],
)
def test_failures_are_typed_without_exposing_raw_detail(payload, returncode, stderr, error_code):
    with pytest.raises(PocError, match=error_code):
        parse_envelope(completed(payload, returncode=returncode, stderr=stderr))


def test_non_json_output_is_rejected():
    with pytest.raises(PocError, match="antigravity_invalid_envelope"):
        parse_envelope(subprocess.CompletedProcess([], 0, "not-json", ""))
