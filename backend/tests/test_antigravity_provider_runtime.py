from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.ai_gateway import AiGateway, AiProviderCredentials
from app.services.antigravity_provider_client import (
    AntigravityProviderClient,
    AntigravityProviderPreCallError,
    AntigravityProviderResultUnknown,
)
from app.services.task_center.ai_structured_provider_runtime import StructuredProviderRequest
from app.services.task_center.ai_structured_provider_runtime import _candidate_request_id
from app.services.task_center.ai_structured_provider_runtime import structured_failure_outcome
from app.models import AiProvider
from app.services.task_center.ai_provider_candidate_runtime import (
    ProviderDraftRequest,
    _candidate_request_id as _draft_candidate_request_id,
    route_transport_failure,
)
from app.services.task_center.ai_generator import _provider_request_id
from app.services.task_center.two_stage_generation import _realizer_config
from app.services.task_center.message_brief import MessageBrief
from scripts.antigravity_provider_ledger import RequestLedger
from scripts.antigravity_provider_server import AntigravityRuntime, BridgeConfig, BridgeError


pytestmark = pytest.mark.no_postgres


def credentials(model: str = "gemini-3.6-flash-medium") -> AiProviderCredentials:
    return AiProviderCredentials(
        provider_name="slot-01",
        provider_type="antigravity_cli",
        base_url="http://host.docker.internal:18101",
        model_name=model,
        api_key="bridge-token",
    )


def test_draft_candidate_request_identity_binds_route_item() -> None:
    request = ProviderDraftRequest(
        prompt="prompt", count=1, topic="topic", tone="tone", persona_set=(),
        temperature=0.5, max_tokens=100, system_prompt=None, timeout=30,
        request_id="agy:job:stage",
    )
    primary = _draft_candidate_request_id(request, 11, 1, credentials())
    assert primary.startswith("agy:job:stage:i")
    assert _draft_candidate_request_id(request, 12, 2, credentials()) != primary


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._raw


def test_client_returns_structured_output_and_real_usage(monkeypatch):
    payload = {
        "state": "confirmed",
        "structured_output": {"reply": "在呢"},
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(payload))
    result = AntigravityProviderClient().generate(
        credentials(), request_id="job-1", system_prompt="s", user_prompt="u",
        json_schema={"type": "object"}, timeout=30,
    )
    assert result.payload == {"reply": "在呢"}
    assert result.usage.total_tokens == 12


def test_client_check_requires_both_models_and_ready_health(monkeypatch):
    calls: list[tuple[str, str]] = []

    def respond(request, **_kwargs):
        calls.append((request.get_method(), request.full_url))
        if request.get_method() == "GET":
            return FakeResponse({
                "status": "ready", "binary_ready": True, "cli_version": "agy 1",
                "process_state": "idle", "last_terminal_code": "confirmed",
                "quota_limited": False, "schema_probe_age_seconds": 0,
                "probe_max_age_seconds": 300,
                "model_visibility": {
                    "gemini-3.6-flash-medium": True,
                    "gemini-3.1-pro-low": True,
                },
            })
        body = json.loads(request.data)
        return FakeResponse({
            "state": "confirmed", "model": body["model"],
            "structured_output": {"status": "ok"}, "usage": {},
        })

    monkeypatch.setattr("urllib.request.urlopen", respond)
    ok, detail = AntigravityProviderClient().check(credentials(), timeout=30)
    assert ok is True
    assert "models/schema/health ready" in detail
    assert [method for method, _url in calls] == ["POST", "POST", "GET"]


def test_client_check_rejects_degraded_health(monkeypatch):
    responses = iter((
        {"state": "confirmed", "structured_output": {"status": "ok"}, "usage": {}},
        {"state": "confirmed", "structured_output": {"status": "ok"}, "usage": {}},
        {
            "status": "degraded", "binary_ready": True, "cli_version": "agy 1",
            "process_state": "idle", "last_terminal_code": "confirmed",
            "quota_limited": False, "schema_probe_age_seconds": 999,
            "probe_max_age_seconds": 300, "model_visibility": {},
        },
    ))
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(next(responses)),
    )
    ok, detail = AntigravityProviderClient().check(credentials(), timeout=30)
    assert ok is False
    assert "health_not_ready" in detail


def test_provider_started_unknown_never_enters_route_failover(monkeypatch):
    payload = {"state": "unknown", "error_code": "antigravity_provider_result_unknown"}
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(payload))
    with pytest.raises(AntigravityProviderResultUnknown) as raised:
        AntigravityProviderClient().generate(
            credentials(), request_id="job-2", system_prompt="s", user_prompt="u",
            json_schema={"type": "object"}, timeout=30,
        )
    assert route_transport_failure(raised.value) is False


def test_gateway_uses_explicit_antigravity_provider(monkeypatch):
    captured = {}

    def fake_generate(_self, _credentials, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(payload={"reply": "可以"}, usage=SimpleNamespace(total_tokens=9))

    monkeypatch.setattr(AntigravityProviderClient, "generate", fake_generate)
    payload, usage = AiGateway().generate_structured(
        credentials("gemini-3.1-pro-low"), "user", temperature=None,
        max_tokens=None, system_prompt="system", request_id="generation-job-1",
    )
    assert payload == {"reply": "可以"}
    assert usage.total_tokens == 9
    assert captured["request_id"] == "generation-job-1"


def test_antigravity_draft_schema_freezes_prompt_slots(monkeypatch):
    captured = {}
    slot_ids = ["task-1:cycle:2:turn:1", "task-1:cycle:2:turn:2"]
    prompt = _fixed_slot_prompt(slot_ids)

    def fake_generate(_self, _credentials, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            payload={"drafts": [
                {"slot_id": slot_ids[0], "content": "第一条"},
                {"slot_id": slot_ids[1], "content": "第二条"},
            ]},
            usage=SimpleNamespace(total_tokens=9),
        )

    monkeypatch.setattr(AntigravityProviderClient, "generate", fake_generate)
    result = AiGateway().generate_drafts(
        credentials(), prompt, count=2, topic="群聊", tone="自然",
        persona_set=["A", "B"], temperature=None, max_tokens=None,
        request_id="generation-job-1",
    )

    drafts = captured["json_schema"]["properties"]["drafts"]
    assert (drafts["minItems"], drafts["maxItems"]) == (2, 2)
    assert drafts["items"]["required"] == ["content", "slot_id"]
    assert drafts["items"]["properties"]["slot_id"]["enum"] == slot_ids
    assert [candidate.slot_id for candidate in result.candidates] == slot_ids


def test_antigravity_non_slot_draft_keeps_generic_schema(monkeypatch):
    captured = {}

    def fake_generate(_self, _credentials, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            payload={"drafts": [{"content": "普通候选"}]},
            usage=SimpleNamespace(total_tokens=7),
        )

    monkeypatch.setattr(AntigravityProviderClient, "generate", fake_generate)
    result = AiGateway().generate_drafts(
        credentials(), "普通提示", count=1, topic="群聊", tone="自然",
        persona_set=["A"], temperature=None, max_tokens=None,
        request_id="generation-job-2",
    )

    item = captured["json_schema"]["properties"]["drafts"]["items"]
    assert item["required"] == ["content"]
    assert result.candidates[0].slot_id == ""


def test_antigravity_fixed_slot_response_still_rejects_missing_slot(monkeypatch):
    def fake_generate(_self, _credentials, **_kwargs):
        return SimpleNamespace(
            payload={"drafts": [{"content": "缺少槽位"}]},
            usage=SimpleNamespace(total_tokens=7),
        )

    monkeypatch.setattr(AntigravityProviderClient, "generate", fake_generate)
    with pytest.raises(RuntimeError, match="fixed_slot_contract=slot_mapping"):
        AiGateway().generate_drafts(
            credentials(), _fixed_slot_prompt(["task-1:cycle:2:turn:1"]),
            count=1, topic="群聊", tone="自然", persona_set=["A"],
            temperature=None, max_tokens=None, request_id="generation-job-3",
        )


def _fixed_slot_prompt(slot_ids: list[str]) -> str:
    payload = {
        "generation_slots": [
            {"slot_id": slot_id, "sequence_index": index}
            for index, slot_id in enumerate(slot_ids, 1)
        ],
    }
    return (
        "Sanitized production-shaped input:\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"Generate exactly {len(slot_ids)} Chinese draft(s)."
    )


def test_gateway_rejects_unsupported_antigravity_sampling_parameters():
    with pytest.raises(RuntimeError, match="unsupported_provider_parameter"):
        AiGateway().generate_structured(
            credentials(), "user", temperature=0.7, max_tokens=128,
            system_prompt="system", request_id="generation-job-1",
        )


def test_structured_request_id_is_stable_per_prompt_and_distinct_between_calls():
    base = dict(
        config={
            "_generation_job_id": "job-1",
            "_ai_provider_route_set_revision": 3,
            "_ai_provider_invocation_key": "realizer:slot-1:attempt:1",
        },
        temperature=0.7, max_tokens=128, count=1,
        purpose="group_realize_general", model_name="gemini-3.6-flash-medium",
        stage="primary", required_model_family="antigravity",
    )
    first = StructuredProviderRequest(system_prompt="s", user_prompt="prompt-a", **base)
    retry = StructuredProviderRequest(system_prompt="s", user_prompt="prompt-a", **base)
    second = StructuredProviderRequest(
        system_prompt="s", user_prompt="prompt-b",
        **{
            **base,
            "config": {
                **base["config"],
                "_ai_provider_invocation_key": "realizer:slot-2:attempt:1",
            },
        },
    )
    assert first.request_id() == retry.request_id()
    assert first.request_id() != second.request_id()
    primary = _candidate_request_id(first, 11, 1, "gemini-3.6-flash-medium")
    secondary = _candidate_request_id(first, 12, 2, "gemini-3.1-pro-low")
    assert primary != secondary
    assert _candidate_request_id(retry, 11, 1, "gemini-3.6-flash-medium") == primary
    assert _candidate_request_id(first, 13, 2, "gemini-3.6-flash-medium") != primary


def test_draft_request_id_uses_durable_slots_not_prompt_text():
    config = {
        "_generation_job_id": "job-1",
        "_ai_provider_route_set_revision": 4,
        "generation_slots": [{"slot_id": "slot-1"}, {"slot_id": "slot-2"}],
    }
    first = _provider_request_id(config, "group_chat_message", "primary")
    retry = _provider_request_id(dict(config), "group_chat_message", "primary")
    changed = _provider_request_id(
        {**config, "generation_slots": [{"slot_id": "slot-3"}]},
        "group_chat_message", "primary",
    )
    assert first == retry
    assert first != changed


def test_realizer_invocation_identity_is_slot_and_attempt_bound():
    brief = MessageBrief(
        slot_id="slot-1", speech_act="reaction", stance="positive",
        length_band="short", punctuation_profile="none",
    )
    first = _realizer_config({}, brief, 1)["_ai_provider_invocation_key"]
    retry = _realizer_config({}, brief, 1)["_ai_provider_invocation_key"]
    second_attempt = _realizer_config({}, brief, 2)["_ai_provider_invocation_key"]
    assert first == retry == "realizer:slot-1:attempt:1"
    assert second_attempt != first


def test_ledger_is_idempotent_encrypted_and_rejects_hash_drift(tmp_path: Path):
    path = tmp_path / "requests.sqlite3"
    ledger = RequestLedger(path, Fernet.generate_key().decode("ascii"))
    assert ledger.start("request-1", "hash-a").state == "claimed"
    ledger.mark_started("request-1", 123)
    ledger.settle("request-1", state="confirmed", response={"secret": "adult-copy"})
    assert ledger.get("request-1").response == {"secret": "adult-copy"}
    assert b"adult-copy" not in path.read_bytes()
    with pytest.raises(RuntimeError, match="request_id_reused"):
        ledger.start("request-1", "hash-b")


def test_cli_subprocesses_receive_no_bridge_or_ledger_secrets(tmp_path: Path, monkeypatch):
    binary = tmp_path / "agy"
    binary.write_text("""#!/usr/bin/env python3
import json
import os
import sys
observed = {
    "bridge_token": "ANTIGRAVITY_BRIDGE_TOKEN" in os.environ,
    "ledger_key": "ANTIGRAVITY_LEDGER_KEY" in os.environ,
    "home": bool(os.environ.get("HOME")),
}
if "--version" in sys.argv:
    print(json.dumps(observed, sort_keys=True))
else:
    print(json.dumps({
        "status": "SUCCESS", "structured_output": observed,
        "usage": {"total_tokens": 1}, "num_turns": 1,
    }, sort_keys=True))
""")
    binary.chmod(0o755)
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_TOKEN", "must-not-reach-agy")
    monkeypatch.setenv("ANTIGRAVITY_LEDGER_KEY", "must-not-reach-agy")
    config = _runtime_config(tmp_path)
    runtime = AntigravityRuntime(BridgeConfig(**{**config.__dict__, "agy_bin": binary}))
    assert json.loads(runtime.cli_version) == {
        "bridge_token": False, "home": True, "ledger_key": False,
    }
    runtime.ledger.start("request-1", "hash")
    completed = runtime._run_cli("request-1", _request_payload())
    observed = json.loads(completed.stdout)["structured_output"]
    assert observed == {"bridge_token": False, "home": True, "ledger_key": False}


def test_runtime_replays_confirmed_result_without_second_cli_call(tmp_path: Path, monkeypatch):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    calls = []

    def fake_run(request_id, _payload):
        calls.append(1)
        runtime.ledger.mark_started(request_id, 123)
        envelope = {
            "status": "SUCCESS", "structured_output": {"reply": "ok"},
            "usage": {"total_tokens": 20}, "num_turns": 1,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(runtime, "_run_cli", fake_run)
    payload = _request_payload()
    first = runtime.generate(payload)
    second = runtime.generate(payload)
    assert first[1]["state"] == second[1]["state"] == "confirmed"
    assert len(calls) == 1


def test_runtime_spawn_failure_is_not_started_and_same_request_can_retry(tmp_path: Path, monkeypatch):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    calls = 0

    def fake_run(request_id, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError("agy")
        runtime.ledger.mark_started(request_id, 123)
        envelope = {
            "status": "SUCCESS", "structured_output": {"reply": "ok"},
            "usage": {"total_tokens": 1}, "num_turns": 1,
        }
        return subprocess.CompletedProcess([], 0, json.dumps(envelope), "")

    monkeypatch.setattr(runtime, "_run_cli", fake_run)
    with pytest.raises(Exception, match="antigravity_binary_missing"):
        runtime.generate(_request_payload())
    assert runtime.request_status("request-1")[1]["state"] == "not_started"
    assert runtime.generate(_request_payload())[1]["state"] == "confirmed"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("authentication required", "antigravity_auth_required"),
        ("quota exhausted", "antigravity_quota_limited"),
        ("model is not recognized", "antigravity_model_invalid"),
    ],
)
def test_non_json_cli_failure_keeps_typed_error(tmp_path: Path, stderr: str, code: str):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    completed = subprocess.CompletedProcess([], 1, "not-json", stderr)
    with pytest.raises(BridgeError, match=code) as raised:
        runtime._parse_cli("request-1", _request_payload(), completed, 0.0)
    assert raised.value.state == "unknown"


def test_zero_turn_zero_usage_auth_failure_is_proven_pre_call(tmp_path: Path):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    envelope = {
        "status": "ERROR",
        "error": "authentication required",
        "num_turns": 0,
        "usage": {field: 0 for field in (
            "input_tokens", "output_tokens", "thinking_tokens",
            "cache_read_tokens", "total_tokens",
        )},
    }
    completed = subprocess.CompletedProcess([], 1, json.dumps(envelope), "")
    with pytest.raises(BridgeError, match="antigravity_auth_required") as raised:
        runtime._parse_cli("request-1", _request_payload(), completed, 0.0)
    assert raised.value.state == "not_started"


def test_runtime_persists_proven_pre_call_as_retryable_not_started(tmp_path: Path, monkeypatch):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    envelope = {
        "status": "ERROR", "error": "authentication required", "num_turns": 0,
        "usage": {field: 0 for field in (
            "input_tokens", "output_tokens", "thinking_tokens",
            "cache_read_tokens", "total_tokens",
        )},
    }

    def fake_run(request_id, _payload):
        runtime.ledger.mark_started(request_id, 123)
        return subprocess.CompletedProcess([], 1, json.dumps(envelope), "")

    monkeypatch.setattr(runtime, "_run_cli", fake_run)
    with pytest.raises(BridgeError, match="antigravity_auth_required"):
        runtime.generate(_request_payload())
    assert runtime.request_status("request-1")[1]["state"] == "not_started"


@pytest.mark.parametrize(
    "code",
    ["antigravity_auth_required", "antigravity_account_ineligible", "antigravity_quota_limited"],
)
def test_client_maps_proven_pre_call_errors_to_route_retryable(monkeypatch, code: str):
    raw = json.dumps({"state": "not_started", "error_code": code}).encode()

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 422, "", {}, io.BytesIO(raw))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(AntigravityProviderPreCallError) as raised:
        AntigravityProviderClient().generate(
            credentials(), request_id="job-1", system_prompt="s", user_prompt="u",
            json_schema={"type": "object"}, timeout=30,
        )
    assert route_transport_failure(raised.value) is True


def test_structured_quota_failure_marks_provider_and_continues_route():
    provider = AiProvider(
        id=1, provider_name="slot", model_name="gemini-3.6-flash-medium",
        base_url="http://host.docker.internal:18101",
        api_key_ciphertext="cipher", health_status="健康",
    )
    request = StructuredProviderRequest(
        system_prompt="s", user_prompt="u",
        config={
            "_generation_job_id": "job",
            "_ai_provider_route_set_id": "route",
            "_ai_provider_invocation_key": "realizer:slot:attempt:1",
        },
        temperature=0.7, max_tokens=128, count=1,
        purpose="group_realize_general", model_name="gemini-3.6-flash-medium",
        stage="primary", required_model_family="antigravity",
    )
    outcome = structured_failure_outcome(
        SimpleNamespace(), provider, request=request,
        error=AntigravityProviderPreCallError("antigravity_quota_limited"),
        has_more=True,
    )
    assert outcome.continue_candidates is True
    assert outcome.route_retryable is True
    assert provider.health_status == "异常"


def test_started_unknown_quota_never_continues_route():
    provider = AiProvider(
        id=1, provider_name="slot", model_name="gemini-3.6-flash-medium",
        base_url="http://host.docker.internal:18101",
        api_key_ciphertext="cipher", health_status="健康",
    )
    request = StructuredProviderRequest(
        system_prompt="s", user_prompt="u",
        config={
            "_generation_job_id": "job",
            "_ai_provider_route_set_id": "route",
            "_ai_provider_invocation_key": "realizer:slot:attempt:1",
        },
        temperature=0.7, max_tokens=128, count=1,
        purpose="group_realize_general", model_name="gemini-3.6-flash-medium",
        stage="primary", required_model_family="antigravity",
    )
    error = AntigravityProviderResultUnknown("antigravity_quota_limited")
    outcome = structured_failure_outcome(
        SimpleNamespace(), provider, request=request, error=error, has_more=True,
    )
    assert outcome.continue_candidates is False
    assert outcome.route_retryable is False


def test_untyped_bridge_http_500_is_unknown_not_pre_call(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 500, "", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(AntigravityProviderResultUnknown):
        AntigravityProviderClient().generate(
            credentials(), request_id="job-1", system_prompt="s", user_prompt="u",
            json_schema={"type": "object"}, timeout=30,
        )


def _runtime_config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(
        slot_id="slot-01", token="token", agy_bin=Path("/usr/local/bin/agy"),
        ledger_path=tmp_path / "requests.sqlite3",
        ledger_key=Fernet.generate_key().decode("ascii"), max_timeout_seconds=180,
    )


def _request_payload() -> dict:
    return {
        "request_id": "request-1", "model": "gemini-3.6-flash-medium",
        "system_prompt": "system", "user_prompt": "user",
        "json_schema": {"type": "object"}, "timeout_seconds": 30,
    }
