from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import GenerationJob, Task
from app.services._common import _now
from app.services.task_center import ai_generator, comment_generation_worker as worker
from app.services.task_center.ai_generation_claim_lifecycle import mark_generation_claim
from app.services.task_center.legacy_generation_timing import legacy_generation_execution_path
from app.services.task_center.generation_timing_binding import bind_generation_timing_config, TIMING_CONFIG_KEY
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("two_stage", [False, True])
def test_legacy_timing_only_freezes_actual_provider_roles(monkeypatch, two_stage):
    provider = SimpleNamespace(id=1, model_name="QA-realizer", api_key_ciphertext="QA",
                               base_url="https://qa.invalid")
    monkeypatch.setattr(ai_generator, "_resolve_group_generation_provider",
                        lambda *args, **kwargs: (provider, None))
    config = {"ai_two_stage_enabled": two_stage, "ai_model": "QA-realizer",
              "ai_semantic_reviewer_model": "QA-reviewer" if two_stage else ""}
    job = SimpleNamespace(obligation_type="post_comment", prompt_contract_version="QA",
                          example_set_version="QA", voice_profile_version="QA")
    with comment_dispatch_session() as session:
        path = legacy_generation_execution_path(session,
            SimpleNamespace(tenant_id=1, type="channel_comment"), job=job, config=config)
    roles = {role for role, _ in path.provider_routes}
    assert roles == ({"realizer", "router", "reviewer"} if two_stage else {"realizer"})
    snapshot = path.snapshot(adapter="channel_comment", lane="proactive")
    assert ("reviewer_started" in snapshot["stages"]) == two_stage


def test_two_stage_missing_reviewer_still_fails(monkeypatch):
    provider = SimpleNamespace(id=1, model_name="QA", api_key_ciphertext="QA", base_url="https://qa.invalid")
    monkeypatch.setattr(ai_generator, "_resolve_group_generation_provider",
                        lambda *args, **kwargs: (provider, None))
    with comment_dispatch_session() as session:
        with pytest.raises(ValueError, match="reviewer_missing"):
            legacy_generation_execution_path(session,
                SimpleNamespace(tenant_id=1, type="channel_comment"),
                job=SimpleNamespace(), config={"ai_two_stage_enabled": True})


def test_single_stage_comment_reaches_full_unified_timing_binding(monkeypatch):
    provider = SimpleNamespace(id=1, model_name="QA", api_key_ciphertext="QA", base_url="https://qa.invalid")
    monkeypatch.setattr(ai_generator, "_resolve_group_generation_provider",
                        lambda *args, **kwargs: (provider, None))
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        job = GenerationJob(tenant_id=1, task_id=task.id, obligation_type="post_comment",
            obligation_id=action.payload["comment_fulfillment_obligation_id"],
            generation_sequence=1, context_snapshot_version=1,
            latest_safe_send_at=_now() + timedelta(minutes=5))
        session.add(job)
        session.flush()
        result = bind_generation_timing_config(session, task, work=((job, "proactive"),),
            config={"engagement_contract_version": "unified_engagement_v1", "ai_two_stage_enabled": False},
            deadline_at=job.latest_safe_send_at)
        assert result[TIMING_CONFIG_KEY]["provider_calls_allowed"] is True
        assert result[TIMING_CONFIG_KEY]["llm_timeout_ceiling_seconds"] == 15


@pytest.mark.parametrize("failure", ["preparation", "payload"])
def test_unexpected_failure_releases_claim_and_remains_visible(monkeypatch, failure):
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        claim = worker.CommentGenerationClaim(action.id, "QA-owner", "QA-token")
        mark_generation_claim(action, claim.owner, claim.token)
        if failure == "payload":
            action.payload = {**action.payload, "message_id": "not-an-integer"}
        session.commit()
        factory = lambda: Session(session.get_bind())

        def fail(*args, **kwargs):
            raise ValueError("QA-preparation-error")

        monkeypatch.setattr(worker, "ensure_post_comment_content", fail)
        with pytest.raises(ValueError):
            worker._process_comment_generation(factory, claim,
                dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.expire_all()
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "pending"
        assert action.claim_owner == action.claim_token == ""
        assert action.lease_expires_at is None


def test_old_finally_cannot_release_new_owner_or_its_resources(monkeypatch):
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        claim = worker.CommentGenerationClaim(action.id, "old-owner", "old-token")
        mark_generation_claim(action, "new-owner", "new-token")
        session.commit()
        releases = []
        monkeypatch.setattr(worker, "_release_runtime_resources", lambda item: releases.append(item.id))
        factory = lambda: Session(session.get_bind())
        worker._release_comment_generation_claim(factory, claim)
        session.expire_all()
        assert action.status == "executing"
        assert action.claim_owner == "new-owner" and action.claim_token == "new-token"
        assert releases == []


def test_exception_after_durable_provider_start_preserves_unknown(monkeypatch):
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        claim = worker.CommentGenerationClaim(action.id, "QA-owner", "QA-token")
        mark_generation_claim(action, claim.owner, claim.token)
        job = GenerationJob(tenant_id=1, task_id=action.task_id, task_lifecycle_epoch=action.task_lifecycle_epoch,
            obligation_type="post_comment", obligation_id=action.payload["comment_fulfillment_obligation_id"],
            generation_sequence=1, context_snapshot_version=1, state="generating", generation_owner_id=claim.owner)
        session.add(job)
        session.flush()
        action.payload = {**action.payload, "generation_job_id": job.id}
        session.commit()

        def fail_after_provider_started(current, item, **kwargs):
            item.result = {"ai_provider_call_started_at": _now().isoformat()}
            current.commit()
            raise RuntimeError("QA-result-persistence-failed")

        monkeypatch.setattr(worker, "ensure_post_comment_content", fail_after_provider_started)
        factory = lambda: Session(session.get_bind())
        with pytest.raises(RuntimeError, match="QA-result-persistence-failed"):
            worker._process_comment_generation(factory, claim,
                dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.expire_all()
        assert action.payload["ai_generation_status"] == "provider_result_unknown"
        assert job.state == "unknown"
        assert action.claim_owner == action.claim_token == ""
        assert worker._claim_comment_generation(factory, owner="next-worker", excluded_action_ids=set()) is None
