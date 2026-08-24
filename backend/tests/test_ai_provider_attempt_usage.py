from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai_gateway import (
    AiDraftCandidate,
    AiGenerationResult,
    AiGateway,
    AiMalformedStructuredOutputError,
    AiProviderCredentials,
    AiUsage,
)
from app.models import AiProvider
from app.services.task_center import ai_structured_provider_runtime
from app.services.task_center import ai_provider_candidate_runtime
from app.services.task_center.ai_provider_attempts import record_provider_attempt
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.ai_provider_candidate_runtime import (
    DraftAttemptOutcome,
    ProviderCandidatePolicy,
    ProviderDraftRequest,
    generate_with_provider_candidates,
    raise_provider_generation_failure,
)
from app.services.task_center.ai_structured_provider_runtime import (
    StructuredProviderRequest,
    attempt_structured_candidate,
)


pytestmark = pytest.mark.no_postgres


class _CaptureSession:
    def __init__(self) -> None:
        self.added = []

    def scalar(self, _statement):
        return 0

    def add(self, row) -> None:
        self.added.append(row)

    def commit(self) -> None:
        return None


def _provider() -> AiProvider:
    return AiProvider(
        id=7,
        provider_name="metered",
        base_url="https://provider.invalid",
        model_name="model-a",
        api_key_ciphertext="ciphertext",
        input_price_per_1k=2.0,
        output_price_per_1k=4.0,
        currency="CNY",
        is_billable=True,
    )


def test_provider_attempt_records_split_usage_and_cost() -> None:
    session = _CaptureSession()

    record_provider_attempt(
        session,
        {"_generation_job_id": "job-1", "_ai_provider_route_set_id": "route-1"},
        _provider(),
        purpose="group_realize_general",
        priority=1,
        model_name="model-a",
        request_text="prompt",
        outcome="success",
        usage=AiUsage(
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=20,
            total_tokens=150,
            billable=True,
        ),
    )

    row = session.added[0]
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 50
    assert row.cached_tokens == 20
    assert row.cost_amount == pytest.approx(0.4)
    assert row.currency == "CNY"


def test_legacy_provider_attempt_allows_explicit_empty_route() -> None:
    session = _CaptureSession()

    record_provider_attempt(
        session,
        {"_generation_job_id": "job-1"},
        _provider(),
        purpose="group_chat_message",
        priority=1,
        model_name="model-a",
        request_text="prompt",
        outcome="success",
        usage=AiUsage(total_tokens=12),
    )

    row = session.added[0]
    assert row.route_set_id is None
    assert row.route_set_revision == 0


def test_legacy_draft_runtime_records_real_provider_usage(monkeypatch) -> None:
    provider = _provider()
    credentials = AiProviderCredentials(
        provider_name="metered",
        provider_type="openai_compatible",
        base_url="https://provider.invalid",
        model_name="model-a",
        api_key="secret",
    )
    usage = AiUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11)
    captured = {}
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "draft_provider_calls",
        lambda *_args: ([provider], iter([(provider, credentials)])),
    )
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "attempt_provider_draft",
        lambda *_args, **_kwargs: DraftAttemptOutcome(
            AiGenerationResult([AiDraftCandidate("群友", "今天聊点啥")], usage),
            None,
            False,
            False,
        ),
    )
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "record_provider_attempt",
        lambda *_args, **kwargs: captured.update(kwargs),
    )

    result = generate_with_provider_candidates(
        SimpleNamespace(),
        provider,
        ProviderDraftRequest("user", 1, "", "", (), 0.7, 64, "system", 30),
        policy=ProviderCandidatePolicy(
            "model-a", "", False, "group_chat_message", False,
            attempt_config={"_generation_job_id": "job-1"},
        ),
    )

    assert result.usage == usage
    assert captured["usage"] == usage
    assert captured["outcome"] == "success"


@pytest.mark.parametrize(
    ("usage_payload", "expected"),
    [
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
            60,
        ),
        (
            {
                "prompt_tokens": 80,
                "completion_tokens": 5,
                "cache_read_input_tokens": 40,
            },
            40,
        ),
    ],
)
def test_gateway_extracts_provider_cache_usage(usage_payload, expected) -> None:
    usage = AiGateway()._usage_from_payload({"usage": usage_payload})

    assert usage.cached_tokens == expected


def test_structured_attempt_keeps_usage_until_attempt_is_recorded(monkeypatch) -> None:
    usage = AiUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    captured = {}
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "begin_provider_call",
        lambda _provider: None,
    )
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "call_structured_provider",
        lambda *_args, **_kwargs: ({"briefs": []}, usage),
    )
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "record_attempt",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    request = StructuredProviderRequest(
        system_prompt="system",
        user_prompt="user",
        config={},
        temperature=0.7,
        max_tokens=128,
        count=1,
        purpose="group_context_route",
        model_name="model-a",
        stage="routing",
        required_model_family="",
    )

    outcome = attempt_structured_candidate(
        SimpleNamespace(),
        _provider(),
        object(),
        request=request,
        priority=1,
        model_name="model-a",
        has_more=False,
    )

    assert outcome.result == ({"briefs": []}, 18)
    assert captured["usage"] == usage


def test_malformed_structured_json_is_typed_and_never_rotated(monkeypatch) -> None:
    gateway = AiGateway()
    monkeypatch.setattr(
        gateway,
        "_post_openai_compatible",
        lambda *_args, **_kwargs: ("not-json", AiUsage(total_tokens=9)),
    )
    credentials = AiProviderCredentials(
        provider_name="provider",
        provider_type="openai_compatible",
        base_url="https://provider.invalid",
        model_name="model-a",
        api_key="secret",
    )

    with pytest.raises(AiMalformedStructuredOutputError) as exc_info:
        gateway.generate_structured(
            credentials,
            "prompt",
            temperature=0.2,
            max_tokens=64,
            system_prompt="system",
        )
    assert exc_info.value.usage.total_tokens == 9
    with pytest.raises(AiGenerationUnavailable, match="^malformed_output$"):
        raise_provider_generation_failure(
            exc_info.value,
            "两阶段意图规划",
        )


def test_malformed_attempt_records_consumed_usage(monkeypatch) -> None:
    usage = AiUsage(
        prompt_tokens=20,
        completion_tokens=4,
        total_tokens=24,
        billable=True,
    )
    captured = {}
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "begin_provider_call",
        lambda _provider: object(),
    )
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "call_structured_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AiMalformedStructuredOutputError("invalid", usage=usage)
        ),
    )
    monkeypatch.setattr(
        ai_structured_provider_runtime,
        "record_attempt",
        lambda *_args, **kwargs: captured.update(kwargs),
    )

    outcome = attempt_structured_candidate(
        SimpleNamespace(),
        _provider(),
        object(),
        request=StructuredProviderRequest(
            system_prompt="system",
            user_prompt="user",
            config={"_ai_provider_route_set_id": "route-1"},
            temperature=0.7,
            max_tokens=128,
            count=1,
            purpose="group_context_route",
            model_name="model-a",
            stage="routing",
            required_model_family="",
        ),
        priority=1,
        model_name="model-a",
        has_more=True,
    )

    assert outcome.continue_candidates is False
    assert captured["outcome"] == "failed"
    assert captured["usage"] == usage
