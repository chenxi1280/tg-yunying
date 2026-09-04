from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import GenerationJob, GenerationTimingBinding, Task, Tenant
from app.services.task_center import generation_timing_binding as binding
from app.services.task_center.ai_provider_routes import (
    COMMENT_REALIZE_PURPOSE, COMMENT_REVIEW_PURPOSE, COMMENT_ROUTE_PURPOSE,
    GROUP_REVIEW_PURPOSE, GROUP_ROUTE_PURPOSE, REALIZE_PURPOSE_BY_MODE,
)
from app.services.task_center.engagement_timing_measurements import TimingSampleInput, record_execution_timing_sample
from app.services.task_center.engagement_timing_profiles import TimingProfileApproval, publish_execution_timing_profile
from app.services.task_center.generation_timing_path import generation_execution_path


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 4, 12)
CONFIG = {"engagement_contract_version": "unified_engagement_v1", "ai_content_route_v2_enabled": True}


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(binding, "_now", lambda: NOW)
    with Session(engine, autoflush=False) as current:
        current.add(Tenant(id=1, name="QA timing only"))
        current.flush()
        yield current
    engine.dispose()


def _job(session, *, adapter="group_ai_chat", identity="one"):
    task = session.get(Task, adapter)
    if task is None:
        task = Task(id=adapter, tenant_id=1, name="QA", type=adapter, type_config=CONFIG)
        session.add(task)
        session.flush()
    purposes = (GROUP_ROUTE_PURPOSE, REALIZE_PURPOSE_BY_MODE["general"], GROUP_REVIEW_PURPOSE)
    if adapter == "channel_comment":
        purposes = (COMMENT_ROUTE_PURPOSE, COMMENT_REALIZE_PURPOSE, COMMENT_REVIEW_PURPOSE)
    job = GenerationJob(
        id=identity, tenant_id=1, task_id=task.id, obligation_type="QA", obligation_id=identity,
        generation_sequence=1, context_snapshot_version=1, content_mode="general",
        prompt_contract_version="QA-prompt-v1", example_set_version="QA-example-v1", voice_profile_version="QA-voice-v1",
        latest_safe_send_at=NOW + timedelta(seconds=60),
        provider_route_snapshots={purpose: {"route_set_id": purpose, "revision": 1, "content_hash": "a" * 64} for purpose in purposes},
    )
    session.add(job)
    session.flush()
    return task, job


def _approve(session, task, job, *, lane="response", reference="first"):
    path = generation_execution_path(job, adapter=task.type, config=CONFIG)
    start = NOW - timedelta(minutes=2)
    boundaries = {"pre_materialization": start, "pre_provider": start + timedelta(seconds=1),
                  "ready_action": start + timedelta(seconds=10), "gateway_call_issued": start + timedelta(seconds=11)}
    if task.type == "channel_comment":
        boundaries["reviewer_started"] = start + timedelta(seconds=7)
    sample = record_execution_timing_sample(session, TimingSampleInput(
        tenant_id=1, adapter=task.type, lane=lane, evidence_kind="shadow_run", evidence_reference=f"QA-{reference}",
        evidence_hash="a" * 64, execution_path=path, boundaries=boundaries,
    ))
    approval = TimingProfileApproval(
        tenant_id=1, adapter=task.type, lane=lane, sample_ids=(sample.id,), minimum_sample_count=1,
        approved_by="QA fixture", approval_reference=reference, effective_at=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(days=1), execution_path=path,
    )
    return publish_execution_timing_profile(session, approval), approval


def _bind(session, task, job, *, deadline=None, lane="response"):
    return binding.bind_generation_timing_config(
        session, task, work=((job, lane),), config=CONFIG, deadline_at=deadline or job.latest_safe_send_at,
    )[binding.TIMING_CONFIG_KEY]


@pytest.mark.parametrize("adapter", ("group_ai_chat", "channel_comment"))
@pytest.mark.parametrize("lane", ("response", "proactive"))
def test_binding_needs_no_historical_profile_or_policy(session, adapter, lane):
    task, job = _job(session, adapter=adapter)
    result = _bind(session, task, job, lane=lane)
    saved = session.get(GenerationTimingBinding, job.id)
    assert saved.timing_profile_id is None
    assert saved.profile_snapshot_hash is None
    assert saved.resilience_policy_id is None
    assert result["timing_policy"] == "deadline_only_v1"
    assert result["candidate_ready_deadline_at"] == (NOW + timedelta(seconds=59)).isoformat()
    assert result["llm_timeout_ceiling_seconds"] == 15
    assert binding.TIMING_CONFIG_KEY not in CONFIG


def test_later_deadline_does_not_extend_original_binding(session):
    task, job = _job(session)
    first = _bind(session, task, job)
    job.latest_safe_send_at = NOW + timedelta(hours=1)
    assert _bind(session, task, job) == first
    assert session.scalar(select(func.count()).select_from(GenerationTimingBinding)) == 1
    shorter = _bind(session, task, job, deadline=NOW + timedelta(seconds=30))
    assert shorter["candidate_ready_deadline_at"] == (NOW + timedelta(seconds=29)).isoformat()
    assert _bind(session, task, job) == shorter


def test_old_profile_is_preserved_but_no_longer_a_runtime_gate(session):
    task, job = _job(session)
    profile, _ = _approve(session, task, job)
    _bind(session, task, job)
    saved = session.get(GenerationTimingBinding, job.id)
    saved.timing_profile_id = profile.id
    saved.profile_snapshot_hash = "old-profile-hash"
    profile.state = "superseded"
    profile.valid_until = NOW - timedelta(days=1)
    result = _bind(session, task, job)
    assert result["provider_calls_allowed"] is True
    assert saved.timing_profile_id == profile.id
    assert saved.profile_snapshot_hash == "old-profile-hash"


@pytest.mark.parametrize("remaining", (2, 5, 14))
def test_short_current_window_is_not_rejected_by_historical_p95(session, remaining):
    task, job = _job(session)
    job.latest_safe_send_at = NOW + timedelta(seconds=remaining)
    result = _bind(session, task, job)
    from app.services.task_center.generation_invocation_budget import provider_invocation_timeout
    config = {**CONFIG, binding.TIMING_CONFIG_KEY: result}
    assert provider_invocation_timeout(config, legacy_timeout=30, now_value=NOW) == remaining - 1


def test_lightweight_binding_prevents_unsafe_schema_downgrade(session, monkeypatch):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    task, job = _job(session)
    _bind(session, task, job)
    migration = import_module("migrations.versions.0219_lightweight_timing")
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(session.connection())))
    with pytest.raises(RuntimeError, match="downgrade_requires_historical_bindings"):
        migration.downgrade()
    assert session.get(GenerationTimingBinding, job.id).timing_profile_id is None


def test_batch_failure_rolls_back_all_new_bindings_without_erasing_jobs(session):
    task, first = _job(session, identity="a")
    _, second = _job(session, identity="b")
    second.task_lifecycle_epoch = 2
    with pytest.raises(ValueError, match="job_scope_mismatch"):
        binding.bind_generation_timing_config(session, task, work=((first, "response"), (second, "response")),
                                              config=CONFIG, deadline_at=first.latest_safe_send_at)
    session.commit()
    assert session.scalar(select(func.count()).select_from(GenerationTimingBinding)) == 0
    assert session.scalar(select(func.count()).select_from(GenerationJob)) == 2


@pytest.mark.parametrize("failure", ("late", "missing_deadline", "scope"))
def test_invalid_admission_never_leaves_a_binding(session, failure):
    task, job = _job(session)
    if failure == "late":
        job.latest_safe_send_at = NOW + timedelta(seconds=1)
    elif failure == "missing_deadline":
        job.latest_safe_send_at = None
    else:
        job.task_lifecycle_epoch = 2
    with pytest.raises(ValueError, match="generation_timing_"):
        _bind(session, task, job)
    session.commit()
    assert session.scalar(select(func.count()).select_from(GenerationTimingBinding)) == 0


def test_frozen_path_change_is_rejected(session):
    task, job = _job(session)
    _bind(session, task, job)
    job.voice_profile_version = "changed-voice"
    with pytest.raises(ValueError, match="binding_changed"):
        _bind(session, task, job)


def test_legacy_config_is_unchanged_and_unified_missing_work_is_explicit(session):
    assert binding.bind_generation_timing_config(session, None, work=(), config={}, deadline_at=None) == {}
    with pytest.raises(ValueError, match="frozen_jobs_or_routes_missing"):
        binding.bind_generation_timing_config(session, None, work=(), config=CONFIG, deadline_at=None)


def test_cached_recovery_preserves_tail_without_charging_provider_again(session, monkeypatch):
    task, job = _job(session)
    _bind(session, task, job)
    monkeypatch.setattr(binding, "_now", lambda: NOW + timedelta(seconds=50))
    result = binding.bind_generation_timing_config(
        session, task, work=((job, "response"),), config=CONFIG, deadline_at=job.latest_safe_send_at, requires_provider=False,
    )[binding.TIMING_CONFIG_KEY]
    assert result["provider_calls_allowed"] is False
    assert result["candidate_ready_deadline_at"] == (NOW + timedelta(seconds=59)).isoformat()
    monkeypatch.setattr(binding, "_now", lambda: NOW + timedelta(seconds=59))
    with pytest.raises(ValueError, match="deadline_missed"):
        binding.bind_generation_timing_config(session, task, work=((job, "response"),), config=CONFIG,
                                              deadline_at=job.latest_safe_send_at, requires_provider=False)


def test_cached_recovery_without_original_binding_cannot_invent_one(session):
    task, job = _job(session)
    with pytest.raises(ValueError, match="recovery_binding_missing"):
        binding.bind_generation_timing_config(session, task, work=((job, "response"),), config=CONFIG,
                                              deadline_at=job.latest_safe_send_at, requires_provider=False)


@pytest.mark.parametrize("change", (
    {"ai_two_stage_enabled": True}, {"generation_slots": [{}, {}]}, {"_ai_group_model_fallback_enabled": False},
))
def test_call_shape_is_frozen_without_needing_a_timing_profile(session, change):
    task, job = _job(session)
    result = binding.bind_generation_timing_config(session, task, work=((job, "response"),), config={**CONFIG, **change},
                                                  deadline_at=job.latest_safe_send_at)
    assert result[binding.TIMING_CONFIG_KEY]["provider_calls_allowed"] is True
    with pytest.raises(ValueError, match="binding_changed"):
        _bind(session, task, job)


def test_group_runtime_builder_reaches_real_timing_binding(session, monkeypatch):
    from app.models import Action
    from app.services.task_center import ai_generation_runtime_config as runtime
    from app.services.task_center import ai_group_content_allocation
    from app.services.task_center.payloads import SendMessagePayload

    task, job = _job(session)
    payload = SendMessagePayload(group_id=7, generation_job_id=job.id, reply_to_message_id=11, ai_generation_status="pending")
    action = Action(id="qa-action", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message")
    monkeypatch.setattr(ai_group_content_allocation, "validate_content_intent_for_gateway", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "bind_group_generation_contracts", lambda *args, **kwargs: kwargs["config"])
    monkeypatch.setattr(runtime, "bind_generation_job_routes", lambda _s, _j, config, **kwargs: config)
    monkeypatch.setattr(runtime, "enrich_group_generation_slots", lambda _c, _b, slots: slots)
    result = runtime.build_runtime_config(session, task, [(action, payload)], generation_slot_builder=lambda *args, **kwargs: {})
    assert result[binding.TIMING_CONFIG_KEY]["bindings"][0]["generation_job_id"] == job.id
    assert result[binding.TIMING_CONFIG_KEY]["provider_calls_allowed"] is True


def test_comment_runtime_builder_reaches_real_timing_binding(session, monkeypatch):
    from app.models import Action
    from app.services.task_center import comment_generation_dispatch as runtime
    from app.services.task_center.channel_payloads import PostCommentPayload

    task, job = _job(session, adapter="channel_comment")
    action = Action(id="qa-comment", tenant_id=1, task_id=task.id, task_type=task.type, action_type="post_comment")
    payload = PostCommentPayload(channel_target_id=1, channel_message_id=1, channel_id="qa-channel", message_id=11,
                                 ai_generation_status="pending")
    monkeypatch.setattr(runtime, "bind_comment_generation_contract", lambda *args, **kwargs: kwargs["config"])
    monkeypatch.setattr(runtime, "bind_generation_job_routes", lambda _s, _j, config, **kwargs: config)
    result = runtime._generation_config(session, task, action, payload=payload, job=job)
    assert result[binding.TIMING_CONFIG_KEY]["bindings"][0]["generation_job_id"] == job.id
    assert result[binding.TIMING_CONFIG_KEY]["provider_calls_allowed"] is True
