import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import ai_gateway
from app.ai_gateway import AiGateway, AiProviderCredentials, AiRequestDeadlineExceeded
from app.ai_http_transport import AiHttpResultUnknown
from app.services.antigravity_provider_client import AntigravityProviderClient, ANTIGRAVITY_PRIMARY_MODEL
from app.services.task_center import ai_structured_provider_runtime as structured
from app.services.task_center import generation_invocation_budget as budget
from tests.ai_http_test_support import HTTP_IO_TEST_BUDGET_SECONDS, HTTP_SCHEDULING_TOLERANCE_SECONDS, local_http_server


pytestmark = pytest.mark.no_postgres


def _credentials(url):
    return AiProviderCredentials("QA", "openai_compatible", url, "mimo-v2.5", "qa-test-key")


def _draft(gateway, url, *, deadline):
    return gateway.generate_drafts(_credentials(url), "请输出 json drafts", count=1, topic="QA", tone="简短",
        persona_set=["AI 助手"], temperature=0.7, max_tokens=512, timeout=10, request_deadline=deadline)


def test_real_gateway_uses_bounded_http_and_returns_parsed_drafts(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with local_http_server() as (url, observed):
        result = _draft(AiGateway(), url + "/gateway", deadline=time.monotonic() + 2)
    assert [item.content for item in result.candidates] == ["QA 自动化测试回复"]
    assert len(observed) == 1


@pytest.mark.parametrize("kind", ("reasoning", "parse"))
def test_internal_retries_share_deadline_instead_of_receiving_full_budget(monkeypatch, kind):
    deadline = time.monotonic() + 2
    seen = []
    final = {"choices": [{"message": {"content": '{"drafts":[{"content":"QA 完整回复"}]}'}}]}
    first = {"choices": [{"message": {"content": "" if kind == "reasoning" else '{"drafts":[',
                                     "reasoning_content": "QA reasoning"}, "finish_reason": "length"}]}
    responses = [first, final]

    def response(request, *, timeout, request_deadline):
        seen.append((timeout, request_deadline))
        return json.dumps(responses.pop(0)).encode()

    monkeypatch.setattr(ai_gateway, "read_http", response)
    result = _draft(AiGateway(), "http://localhost/unused", deadline=deadline)
    assert [item.content for item in result.candidates] == ["QA 完整回复"]
    assert [value[1] for value in seen] == [deadline, deadline]
    assert seen[1][0] <= seen[0][0] < 10


def test_expired_deadline_cannot_start_internal_retry(monkeypatch):
    seen = []
    now = [100.0]
    monkeypatch.setattr(ai_gateway.time, "monotonic", lambda: now[0])

    def response(*args, **kwargs):
        seen.append(1)
        now[0] = 101.0
        return json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "QA reasoning"},
                                        "finish_reason": "length"}]}).encode()

    monkeypatch.setattr(ai_gateway, "read_http", response)
    with pytest.raises(AiRequestDeadlineExceeded):
        _draft(AiGateway(), "http://localhost/unused", deadline=100.5)
    assert seen == [1]


def test_actual_structured_runtime_supplies_openai_parameters_and_hard_deadline(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(structured, "settle_provider_success", lambda *_: None)
    config = {budget.TIMING_CONFIG_KEY: {"version": "generation_timing_v1", "llm_timeout_ceiling_seconds": 2,
        "candidate_ready_deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()}}
    request = structured.StructuredProviderRequest("QA", "QA", config, 0.7, 512, 1, "QA", "mimo-v2.5", "primary", "")
    with local_http_server() as (url, observed):
        result, _ = structured.call_structured_provider(None, _credentials(url + "/gateway"), request, provider_request_id="qa")
    assert result["drafts"][0]["content"] == "QA 自动化测试回复"
    assert len(observed) == 1


def test_actual_gateway_trickling_response_is_unknown_not_retried(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with local_http_server() as (url, observed):
        started = time.monotonic()
        with pytest.raises(AiHttpResultUnknown) as caught:
            _draft(AiGateway(), url + "/drip", deadline=started + HTTP_IO_TEST_BUDGET_SECONDS)
        assert time.monotonic() - started < HTTP_IO_TEST_BUDGET_SECONDS + HTTP_SCHEDULING_TOLERANCE_SECONDS
        assert caught.value.local_termination_confirmed
        assert len(observed) == 1


def test_antigravity_bounded_path_does_not_add_20_seconds(monkeypatch):
    captured = []

    def request(*args, **kwargs):
        captured.append(kwargs)
        return {"state": "confirmed", "structured_output": {"ok": True}}

    client = AntigravityProviderClient()
    monkeypatch.setattr(client, "_request", request)
    credentials = AiProviderCredentials("QA", "antigravity_cli", "http://localhost", ANTIGRAVITY_PRIMARY_MODEL, "QA")
    deadline = time.monotonic() + 5
    client.generate(credentials, request_id="stable-qa-id", system_prompt="QA", user_prompt="QA", json_schema={},
                    timeout=5, request_deadline=deadline)
    assert captured[0]["timeout"] == 5
    assert captured[0]["request_deadline"] == deadline
    assert captured[0]["payload"]["request_id"] == "stable-qa-id"


def test_real_http_timeout_reaches_worker_unknown_without_replay(monkeypatch):
    from sqlalchemy.orm import Session
    from app.services.task_center import ai_generation_worker
    from tests.test_ai_generation_failure_state import _action_session, _dependencies

    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(ai_generation_worker, "credentials_for_account", lambda *_: object())
    with local_http_server() as (url, observed), _action_session() as (session, action):
        def generate(*args, **kwargs):
            _draft(AiGateway(), url + "/drip", deadline=time.monotonic() + HTTP_IO_TEST_BUDGET_SECONDS)
            raise AssertionError("trickling response unexpectedly completed")

        factory = lambda: Session(session.get_bind(), autoflush=False)
        assert ai_generation_worker.drain_ai_generation(factory, limit=1, dependencies=_dependencies(generate)) == 1
        session.refresh(action)
        assert action.payload["ai_generation_status"] == "provider_result_unknown"
        assert not action.claim_owner
        assert ai_generation_worker.drain_ai_generation(factory, limit=1, dependencies=_dependencies(generate)) == 0
        assert len(observed) == 1


def test_actual_antigravity_gateway_uses_same_bounded_http_transport(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with local_http_server() as (url, observed):
        credentials = AiProviderCredentials("QA", "antigravity_cli", url + "/drip", ANTIGRAVITY_PRIMARY_MODEL, "QA")
        with pytest.raises(AiHttpResultUnknown) as caught:
            AiGateway().generate_structured(credentials, "QA", system_prompt="QA", temperature=None, max_tokens=None,
                                            timeout=5, request_id="qa-stable",
                                            request_deadline=time.monotonic() + HTTP_IO_TEST_BUDGET_SECONDS)
        assert caught.value.local_termination_confirmed
        assert observed[0][0] == "/drip/internal/v1/generate"
