from types import SimpleNamespace

import pytest

from app.ai_gateway import AiProviderRateLimited, AiUsage
from app.ai_transport_errors import AiProviderResultUnknown
from app.models import AiProviderHealthStatus
from app.services.task_center import ai_structured_provider_runtime as runtime
from app.services.task_center import provider_admission as admission
from tests.test_provider_admission import FakeAdmissionRedis, _enable_admission, _provider


pytestmark = pytest.mark.no_postgres
NOW_SECONDS = 1000
RETRY_SECONDS = 90


def _request():
    return runtime.StructuredProviderRequest(
        system_prompt="qa", user_prompt="qa", config={}, temperature=0.2,
        max_tokens=30, count=1, purpose="group_semantic_review", model_name="MiniMax-M3",
        stage="reviewing", required_model_family="",
    )


def _invoke(provider, gateway):
    return runtime.call_structured_provider(
        admission.begin_provider_call(provider), SimpleNamespace(provider_type="openai_compatible"),
        _request(), provider=provider, provider_request_id="qa", gateway=gateway,
    )


@pytest.mark.parametrize("detail", ["rate limit exceeded", "Token Plan usage limit reached (2056)"])
def test_structured_429_defers_shared_calls_then_recovers_after_probe_success(monkeypatch, detail):
    redis = FakeAdmissionRedis()
    _enable_admission(monkeypatch, redis)
    clock = SimpleNamespace(now=NOW_SECONDS)
    monkeypatch.setattr(admission, "_now_epoch", lambda: clock.now)
    provider = _provider()
    provider.health_status = AiProviderHealthStatus.HEALTHY.value
    calls = []

    def generate(*args, **kwargs):
        calls.append(kwargs["request_id"])
        if len(calls) == 1:
            raise AiProviderRateLimited(429, detail, RETRY_SECONDS)
        return {"accepted": True}, AiUsage()

    gateway = SimpleNamespace(generate_structured=generate)
    with pytest.raises(admission.ProviderAdmissionBlocked):
        _invoke(provider, gateway)
    key = admission.provider_admission_key(provider)
    assert redis.hash[key]["source_status"] == "cooldown"
    assert float(redis.hash[key]["retry_at"]) == NOW_SECONDS + RETRY_SECONDS
    assert provider.health_status == AiProviderHealthStatus.HEALTHY.value
    assert not redis.strings
    with pytest.raises(admission.ProviderAdmissionBlocked):
        _invoke(provider, gateway)
    assert len(calls) == 1
    clock.now += RETRY_SECONDS
    payload, _ = _invoke(provider, gateway)
    assert payload == {"accepted": True}
    assert len(calls) == 2
    assert redis.hash[key]["source_status"] == "open"


def test_structured_unknown_remains_unknown_without_marking_cooldown(monkeypatch):
    redis = FakeAdmissionRedis()
    _enable_admission(monkeypatch, redis)
    provider = _provider()

    def generate(*args, **kwargs):
        raise AiProviderResultUnknown("provider_result_unknown")

    with pytest.raises(AiProviderResultUnknown):
        _invoke(provider, SimpleNamespace(generate_structured=generate))
    assert admission.provider_admission_key(provider) not in redis.hash
