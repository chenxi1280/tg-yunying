import pytest

from app.models import AiProvider, AiProviderHealthStatus, GenerationTimingBinding, TenantAiSetting
from app.services.task_center import generation_timing_binding as binding
from app.services.task_center.generation_invocation_budget import provider_invocation_timeout
from tests.test_generation_timing_binding import NOW, _job, session

pytestmark = pytest.mark.no_postgres


def _provider(session):
    session.add(AiProvider(id=71, provider_name="QA", base_url="https://qa.invalid",
        model_name="MiniMax-M3", api_key_ciphertext="encrypted-qa-only", is_active=True,
        health_status=AiProviderHealthStatus.HEALTHY.value))
    session.add(TenantAiSetting(tenant_id=1, ai_enabled=True, default_provider_id=71))
    session.flush()


@pytest.mark.parametrize("adapter", ("group_ai_chat", "channel_comment"))
@pytest.mark.parametrize("two_stage", (False, True))
def test_unified_non_v2_binds_real_deadline_without_v2_routes(session, adapter, two_stage):
    _provider(session)
    task, job = _job(session, adapter=adapter)
    job.provider_route_snapshots = {}
    job.prompt_contract_version = ""
    job.example_set_version = ""
    config = {"engagement_contract_version": "unified_engagement_v1",
        "ai_content_route_v2_enabled": False, "ai_two_stage_enabled": two_stage,
        "ai_provider_id": 71, "ai_model": "MiniMax-M3", "ai_semantic_reviewer_model": "MiniMax-M2.5"}
    result = binding.bind_generation_timing_config(session, task,
        work=((job, "proactive"),), config=config, deadline_at=job.latest_safe_send_at)
    assert provider_invocation_timeout(result, legacy_timeout=90, now_value=NOW) == 15
    assert session.get(GenerationTimingBinding, job.id) is not None
    assert job.provider_route_snapshots == {}
    assert "encrypted-qa-only" not in str(result)


def test_real_group_runtime_builder_loads_jobs_when_v2_is_disabled(session, monkeypatch):
    from app.services.task_center import ai_generation_runtime_config as runtime
    from app.services.task_center import ai_group_content_allocation
    from app.services.task_center.payloads import SendMessagePayload
    from app.models import Action

    _provider(session)
    task, job = _job(session)
    task.fulfillment_contract_version = "fact_first_v3"
    task.type_config = {"engagement_contract_version": "unified_engagement_v1", "ai_content_route_v2_enabled": False}
    job.provider_route_snapshots = {}
    action = session.get(Action, job.id)
    payload = SendMessagePayload(group_id=7, generation_job_id=job.id, ai_generation_status="pending")
    monkeypatch.setattr(ai_group_content_allocation, "validate_content_intent_for_gateway", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "enrich_group_generation_slots", lambda _c, _b, slots: slots)
    result = runtime.build_runtime_config(session, task, [(action, payload)],
        generation_slot_builder=lambda *args, **kwargs: {})
    assert result[binding.TIMING_CONFIG_KEY]["bindings"][0]["generation_job_id"] == job.id
    assert provider_invocation_timeout(result, legacy_timeout=90, now_value=NOW) == 15
