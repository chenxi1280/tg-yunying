from types import SimpleNamespace

import pytest

from app.ai_transport_errors import AiProviderResultUnknown
from app.services.antigravity_provider_client import AntigravityProviderResultUnknown
from app.services.task_center import ai_provider_candidate_runtime as drafts
from app.services.task_center import ai_structured_provider_runtime as structured
from app.services.task_center.ai_generation_contract import GROUP_CHAT_PURPOSE, TWO_STAGE_BRIEF_PURPOSE


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("purpose", (GROUP_CHAT_PURPOSE, TWO_STAGE_BRIEF_PURPOSE, "other"))
@pytest.mark.parametrize("error_type", (AiProviderResultUnknown, AntigravityProviderResultUnknown))
def test_unknown_is_never_wrapped_as_retryable_generation_failure(purpose, error_type):
    error = error_type("quota_limited_but_remote_outcome_unknown")
    with pytest.raises(error_type) as caught:
        drafts.raise_provider_generation_failure(error, purpose)
    assert caught.value is error
    assert drafts.route_transport_failure(error) is False


@pytest.mark.parametrize("kind", ("draft", "structured"))
def test_real_candidate_loop_records_unknown_and_does_not_try_next_provider(monkeypatch, kind):
    module = drafts if kind == "draft" else structured
    providers = [SimpleNamespace(id=index, provider_name="QA", model_name="QA") for index in (1, 2)]
    credentials = SimpleNamespace(model_name="QA", provider_type="openai_compatible")
    calls, records = [], []
    error = AiProviderResultUnknown("http_total_deadline_unknown")

    def gateway(*args, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(module, "begin_provider_call", lambda *_: None)
    monkeypatch.setattr(module, "release_provider_probe", lambda *_: None)
    monkeypatch.setattr(module, "record_provider_attempt", lambda *args, **kwargs: records.append(kwargs))
    monkeypatch.setattr(module.ai_gateway, "generate_drafts" if kind == "draft" else "generate_structured", gateway)
    config = {"_generation_job_id": "qa-job", "_ai_provider_invocation_key": "qa-attempt",
              "_ai_provider_route_provider_ids": [1, 2], "_ai_provider_route_models": {1: "QA", 2: "QA"}}
    monkeypatch.setattr(module, "draft_provider_calls" if kind == "draft" else "structured_provider_calls",
                        lambda *_: (providers, [(provider, credentials) for provider in providers]))
    with pytest.raises(AiProviderResultUnknown) as caught:
        _generate(kind, providers[0], config)
    assert caught.value is error
    assert len(calls) == 1
    assert [record["outcome"] for record in records] == ["provider_result_unknown"]


def _generate(kind, provider, config):
    if kind == "draft":
        request = drafts.ProviderDraftRequest("QA", 1, "QA", "QA", (), 0.7, 100, "QA", 10)
        policy = drafts.ProviderCandidatePolicy("QA", "", False, GROUP_CHAT_PURPOSE, False,
                                               route_provider_ids=(1, 2), attempt_config=config)
        return drafts.generate_with_provider_candidates(None, provider, request, policy=policy)
    request = structured.StructuredProviderRequest("QA", "QA", config, 0.7, 100, 1, TWO_STAGE_BRIEF_PURPOSE, "QA", "primary", "")
    return structured.generate_structured_with_candidates(None, provider, request)


@pytest.mark.parametrize("two_stage", (False, True))
def test_group_pipeline_does_not_retry_unknown_or_turn_it_into_quality_failure(monkeypatch, two_stage):
    from app.services.task_center import ai_generation_pipeline as pipeline

    calls = []

    def unknown(*args, **kwargs):
        calls.append(1)
        raise AiProviderResultUnknown("QA unknown")

    monkeypatch.setattr(pipeline, "plan_message_briefs" if two_stage else "_generate_stage", unknown)
    request = SimpleNamespace(cached_contents=[], config={"ai_two_stage_enabled": two_stage}, batch_ids=["a"],
                              history="QA context", tenant_id=1, is_reply=False, reply_targets=[])
    with pytest.raises(AiProviderResultUnknown):
        pipeline.generate_quality_results(None, request, SimpleNamespace(brief_planner=unknown))
    assert len(calls) == 1


def test_comment_stages_do_not_convert_unknown_to_fallback(monkeypatch):
    from app.services.task_center import comment_generation_pipeline as pipeline

    calls = []

    def unknown(*args, **kwargs):
        calls.append(1)
        raise AiProviderResultUnknown("QA unknown")

    monkeypatch.setattr(pipeline, "_call_generator", unknown)
    with pytest.raises(AiProviderResultUnknown):
        pipeline._run_generation_stages(None, None, None, action_loader=None)
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ("success", "provider_result_unknown"))
@pytest.mark.parametrize("failure_stage", ("lookup", "commit"))
def test_attempt_persistence_failure_after_call_cannot_be_treated_as_safe_retry(outcome, failure_stage):
    from app.services.task_center.ai_provider_attempts import record_provider_attempt

    error = RuntimeError("QA database failure")

    def fail_commit():
        raise error

    session = SimpleNamespace(add=lambda *_: None, commit=fail_commit,
                              scalar=lambda *_: fail_commit() if failure_stage == "lookup" else 0)
    provider = SimpleNamespace(id=1, is_billable=False, currency="USD")
    with pytest.raises(AiProviderResultUnknown) as caught:
        record_provider_attempt(session, {"_generation_job_id": "qa-job"}, provider, purpose="QA", priority=1,
                                model_name="QA", request_text="QA", outcome=outcome)
    assert caught.value.__cause__ is error
