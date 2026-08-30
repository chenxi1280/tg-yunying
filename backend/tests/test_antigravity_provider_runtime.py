from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.ai_gateway import AiGateway, AiProviderCredentials
from app.services.antigravity_provider_client import (
    AntigravityProviderClient,
    AntigravityProviderResultUnknown,
)
from app.services.task_center.ai_provider_candidate_runtime import route_transport_failure
from scripts.antigravity_provider_ledger import RequestLedger
from scripts.antigravity_provider_server import AntigravityRuntime, BridgeConfig


pytestmark = pytest.mark.no_postgres


def credentials(model: str = "gemini-3.5-flash-medium") -> AiProviderCredentials:
    return AiProviderCredentials(
        provider_name="slot-01",
        provider_type="antigravity_cli",
        base_url="http://host.docker.internal:18101",
        model_name=model,
        api_key="bridge-token",
    )


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
        credentials("gemini-3.1-pro-low"), "user", temperature=0.7,
        max_tokens=128, system_prompt="system", request_id="generation-job-1",
    )
    assert payload == {"reply": "可以"}
    assert usage.total_tokens == 9
    assert captured["request_id"].startswith("generation-job-1-")


def test_ledger_is_idempotent_encrypted_and_rejects_hash_drift(tmp_path: Path):
    path = tmp_path / "requests.sqlite3"
    ledger = RequestLedger(path, Fernet.generate_key().decode("ascii"))
    assert ledger.start("request-1", "hash-a").state == "started"
    ledger.settle("request-1", state="confirmed", response={"secret": "adult-copy"})
    assert ledger.get("request-1").response == {"secret": "adult-copy"}
    assert b"adult-copy" not in path.read_bytes()
    with pytest.raises(RuntimeError, match="request_id_reused"):
        ledger.start("request-1", "hash-b")


def test_runtime_replays_confirmed_result_without_second_cli_call(tmp_path: Path, monkeypatch):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))
    calls = []

    def fake_run(_payload):
        calls.append(1)
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


def _runtime_config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(
        slot_id="slot-01", token="token", agy_bin=Path("/usr/local/bin/agy"),
        ledger_path=tmp_path / "requests.sqlite3",
        ledger_key=Fernet.generate_key().decode("ascii"), max_timeout_seconds=180,
    )


def _request_payload() -> dict:
    return {
        "request_id": "request-1", "model": "gemini-3.5-flash-medium",
        "system_prompt": "system", "user_prompt": "user",
        "json_schema": {"type": "object"}, "timeout_seconds": 30,
    }
