from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.ai_gateway import AiGenerationResult, AiUsage
from app.services.task_center import ai_provider_candidate_runtime as drafts
from app.services.task_center import ai_structured_provider_runtime as structured
from app.services.task_center import generation_invocation_budget as budget
from app.services.task_center.ai_generation_contract import AiGenerationUnavailable
from app.services.task_center.ai_generation_pipeline import _require_provider_attempt_budget


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 4, 12)


def _config(seconds=60, *, ceiling=15):
    return {"engagement_contract_version": "unified_engagement_v1", budget.TIMING_CONFIG_KEY: {
        "version": "generation_timing_v1", "llm_timeout_ceiling_seconds": ceiling,
        "candidate_ready_deadline_at": (NOW + timedelta(seconds=seconds)).isoformat(),
    }}


@pytest.mark.parametrize(("seconds", "expected"), ((60, 15), (10.9, 10), (1, 1)))
def test_timeout_intersects_remaining_window_and_ceiling(seconds, expected):
    config = _config(seconds)
    assert budget.provider_invocation_timeout(config, legacy_timeout=120, now_value=NOW) == expected
    assert config[budget.TIMING_CONFIG_KEY]["llm_timeout_ceiling_seconds"] == 15


@pytest.mark.parametrize("seconds", (0.99, 0, -10))
def test_exhausted_window_does_not_authorize_call(seconds):
    with pytest.raises(AiGenerationUnavailable, match="invocation_budget_exhausted"):
        budget.provider_invocation_timeout(_config(seconds), legacy_timeout=120, now_value=NOW)


@pytest.mark.parametrize("ceiling", (0, 16, True, "15"))
def test_invalid_ceiling_is_not_silently_defaulted(ceiling):
    with pytest.raises(AiGenerationUnavailable, match="ceiling_invalid"):
        budget.provider_invocation_timeout(_config(ceiling=ceiling), legacy_timeout=120, now_value=NOW)


def test_missing_snapshot_is_explicit_but_legacy_is_unchanged():
    assert budget.provider_invocation_timeout({}, legacy_timeout=120) == 120
    with pytest.raises(AiGenerationUnavailable, match="snapshot_missing"):
        budget.provider_invocation_timeout({"engagement_contract_version": "unified_engagement_v1"}, legacy_timeout=120)


def test_cached_recovery_cannot_call_provider():
    config = _config()
    config[budget.TIMING_CONFIG_KEY]["provider_calls_allowed"] = False
    with pytest.raises(AiGenerationUnavailable, match="recovery_provider_call_forbidden"):
        budget.provider_invocation_timeout(config, legacy_timeout=120, now_value=NOW)


def test_deadline_offsets_use_same_instant():
    config = _config(10)
    config[budget.TIMING_CONFIG_KEY]["candidate_ready_deadline_at"] = "2026-09-04T04:00:10+00:00"
    assert budget.provider_invocation_timeout(config, legacy_timeout=120, now_value=NOW.replace(tzinfo=timezone(timedelta(hours=8)))) == 10


@pytest.mark.parametrize("kind", ("draft", "structured"))
def test_actual_gateway_receives_budget_and_rechecks_each_call(monkeypatch, kind):
    clock = [NOW]
    captured, released = [], []
    monkeypatch.setattr(budget, "_now", lambda: clock[0])
    module = drafts if kind == "draft" else structured
    monkeypatch.setattr(module, "release_provider_probe", lambda lease: released.append(lease))
    monkeypatch.setattr(module, "settle_provider_success", lambda *_: None)

    def gateway(*args, **kwargs):
        captured.append(kwargs["timeout"])
        return AiGenerationResult([], AiUsage()) if kind == "draft" else ({}, AiUsage())

    method = "generate_drafts" if kind == "draft" else "generate_structured"
    monkeypatch.setattr(module, "ai_gateway", SimpleNamespace(**{method: gateway}))
    credentials = SimpleNamespace(model_name="QA", provider_type="openai_compatible")
    invoke = _invocation(kind, credentials, _config(20))
    invoke()
    clock[0] = NOW + timedelta(seconds=15)
    invoke()
    clock[0] = NOW + timedelta(seconds=20)
    with pytest.raises(AiGenerationUnavailable, match="invocation_budget_exhausted"):
        invoke()
    assert captured == [15, 5]
    assert released == ["QA-lease"]


def _invocation(kind, credentials, config):
    if kind == "draft":
        request = drafts.ProviderDraftRequest("QA", 1, "QA", "QA", (), 0.7, 100, "QA", 120)
        return lambda: drafts.generate_provider_drafts(SimpleNamespace(id=1), credentials, request,
                                                      lease="QA-lease", execution_config=config)
    request = structured.StructuredProviderRequest(config=config, system_prompt="QA", user_prompt="QA", temperature=0.7,
        max_tokens=100, count=1, purpose="QA", model_name="QA", stage="primary", required_model_family="")
    return lambda: structured.call_structured_provider("QA-lease", credentials, request,
                                                       provider=SimpleNamespace(id=1), provider_request_id="QA")


def test_pipeline_uses_remaining_candidate_budget_not_legacy_120_seconds(monkeypatch):
    from app.services.task_center import ai_generation_pipeline

    monkeypatch.setattr(ai_generation_pipeline, "_now", lambda: NOW)
    _require_provider_attempt_budget(SimpleNamespace(config=_config(2)))
    with pytest.raises(AiGenerationUnavailable, match="invocation_budget_exhausted"):
        _require_provider_attempt_budget(SimpleNamespace(config=_config(0)))


def test_generic_comment_entry_passes_execution_config_to_provider_selection(monkeypatch):
    from app.services.task_center import ai_generator

    captured = []

    def generate(*args, **kwargs):
        captured.append(kwargs["execution_config"])
        return [], 0

    monkeypatch.setattr(ai_generator, "generate_contents", generate)
    config = _config()
    ai_generator._generate_channel_attempt(None, 1, config, topic="摄影", requirements="回答问题", attempt=1,
        missing=1, purpose="频道评论", target_label="摄影", message_content="光线如何", adult_context=False)
    assert captured == [config]


def test_generic_unified_entry_uses_frozen_realizer_route(monkeypatch):
    from app.services.task_center import ai_generator

    captured = []

    def resolve(session, tenant, config, **kwargs):
        captured.append((config, kwargs["purpose"]))
        return "qa-provider", "qa-setting", config, "frozen-model"

    monkeypatch.setattr(ai_generator, "_structured_provider_binding", resolve)
    config = _config()
    result = ai_generator._content_generation_provider(SimpleNamespace(scalar=lambda *_: None), 1,
        execution_config=config, provider_id=999, model_name="unrelated-default", required_model_family="")
    assert captured == [(config, ai_generator.TWO_STAGE_REALIZE_PURPOSE)]
    assert result[3] == "frozen-model"


def test_unbound_standalone_grok_cannot_bypass_unified_timing():
    from app.services.task_center import ai_generator

    with pytest.raises(AiGenerationUnavailable, match="grok_route_unbound"):
        ai_generator._generate_grok_stage(None, _config(), None, count=1, purpose="QA", setting=None)
