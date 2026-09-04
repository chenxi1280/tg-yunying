import pytest
from sqlalchemy.orm import Session

from app.models import GenerationJob
from app.services.task_center.channel_payloads import PostCommentPayload
from app.services.task_center.comment_generation_job import (
    CommentGenerationProviderUnresolved, claim_comment_generation_job, invalidate_comment_generation_jobs,
)
from app.services.task_center.comment_generation_pipeline import CommentGenerationDependencies
from app.services.task_center.comment_generation_worker import drain_comment_generation
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


def _seed_job(session, action, *, state="unknown", cache=False):
    job = GenerationJob(tenant_id=action.tenant_id, task_id=action.task_id,
        task_lifecycle_epoch=action.task_lifecycle_epoch, obligation_type="post_comment",
        obligation_id=action.payload["comment_fulfillment_obligation_id"], generation_sequence=1,
        context_snapshot_version=1, state=state, evaluator_evidence={"existing": "must survive"})
    session.add(job)
    session.flush()
    data = {**action.payload, "generation_job_id": job.id, "ai_generation_attempt_id": "QA-original-attempt"}
    if cache:
        data.update(ai_generation_status="ai_result_persist_unknown",
            ai_generation_result_cache={"content": "已经生成的缓存", "tokens": 1, "attempt_id": "QA-original-attempt"})
        action.result = {**dict(action.result or {}), "generation_outcome": "ai_result_persist_unknown"}
    action.payload = data
    session.commit()
    return job


def test_invalidation_preserves_unknown_and_original_evidence():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        job = _seed_job(session, action)
        payload = PostCommentPayload.model_validate(action.payload)
        invalidate_comment_generation_jobs(session, action, payload, reason="source_edited")
        session.commit()
        assert job.state == "unknown"
        assert job.evaluator_evidence == {"existing": "must survive", "invalidation_reason": "source_edited"}
        with pytest.raises(CommentGenerationProviderUnresolved):
            claim_comment_generation_job(session, action, payload, owner="new-worker")


def test_safe_pending_generation_still_invalidates():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        job = _seed_job(session, action, state="pending")
        invalidate_comment_generation_jobs(session, action,
            PostCommentPayload.model_validate(action.payload), reason="source_edited")
        assert job.state == "failed"


@pytest.mark.parametrize("change", ("none", "claimed", "attempt", "job", "invalidated", "empty", "unproven"))
def test_unknown_cache_recovery_requires_original_provenance(change):
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        job = _seed_job(session, action, cache=True)
        data = dict(action.payload)
        if change == "claimed":
            data["ai_generation_status"] = "generating"
        if change == "attempt":
            data["ai_generation_attempt_id"] = "different-attempt"
        if change == "job":
            data["generation_job_id"] = "different-job"
        if change == "invalidated":
            job.evaluator_evidence = {"invalidation_reason": "source_edited"}
        if change == "empty":
            data["ai_generation_result_cache"] = {}
        if change == "unproven":
            action.result = {}
        session.flush()
        payload = PostCommentPayload.model_validate(data)
        if change in {"none", "claimed"}:
            assert claim_comment_generation_job(session, action, payload, owner="recovery").id == job.id
        else:
            with pytest.raises(CommentGenerationProviderUnresolved):
                claim_comment_generation_job(session, action, payload, owner="recovery")


def test_worker_records_unknown_without_provider_and_does_not_reclaim():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        job = _seed_job(session, action)
        action.status = "pending"
        action.lease_owner, action.claim_owner, action.claim_token = "", "", ""
        action.lease_expires_at = None
        session.commit()
        calls = []
        dependencies = CommentGenerationDependencies(
            direct_generator=lambda *args, **kwargs: calls.append(True),
            reply_generator=lambda *args, **kwargs: calls.append(True),
        )
        factory = lambda: Session(session.get_bind())
        assert drain_comment_generation(factory, limit=2, dependencies=dependencies) == 1
        session.expire_all()
        assert action.payload["ai_generation_status"] == "provider_result_unknown"
        assert action.result["error_code"] == "provider_result_unknown"
        assert job.state == "unknown" and calls == []
