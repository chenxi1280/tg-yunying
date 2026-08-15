from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob
from app.services.task_center.comment_generation_dispatch import (
    _persist_generation_failure,
    prepare_comment_generation_request,
    persist_comment_generation_result,
)
from app.services.task_center.comment_generation_job import (
    COMMENT_GENERATION_OBLIGATION_TYPE,
    CommentGenerationJobConflict,
    claim_comment_generation_job,
    finish_comment_generation_job,
)
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


def _open_jobs(session: Session, obligation_id: str) -> list[GenerationJob]:
    return list(session.scalars(select(GenerationJob).where(
        GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
        GenerationJob.obligation_id == obligation_id,
    )))


def test_comment_generation_lifecycle_creates_and_closes_generation_job() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _get_task(session, action)
        request = prepare_comment_generation_request(session, action, task)

        jobs = _open_jobs(session, "comment-obligation-1")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.state == "generating"
        assert job.generation_sequence == 1
        assert job.generation_owner_id == request.claim_owner
        assert len(job.context_snapshot_hash) == 64
        assert job.generation_not_before_at is not None
        assert action.payload["generation_job_id"] == job.id

        persist_comment_generation_result(session, request, "评论正文审计", tokens=3)

        session.expire_all()
        refreshed = session.get(GenerationJob, job.id)
        assert refreshed.state == "ready"
        assert refreshed.generation_owner_id == ""
        assert refreshed.lease_expires_at is None
        assert len(refreshed.candidate_hash) == 64


def test_comment_generation_retry_increments_sequence() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _get_task(session, action)
        request = prepare_comment_generation_request(session, action, task)
        persist_comment_generation_result(session, request, "评论正文审计", tokens=3)
        session.commit()

        # 重开 claim：终结后的义务允许新 job，generation_sequence 递增
        action = session.get(Action, action.id)
        payload = dict(action.payload or {})
        payload.update({"ai_generation_status": "pending", "comment_text": ""})
        action.payload = payload
        action.status = "executing"
        session.commit()
        request2 = prepare_comment_generation_request(session, action, task)

        jobs = _open_jobs(session, "comment-obligation-1")
        assert len(jobs) == 2
        assert {job.generation_sequence for job in jobs} == {1, 2}
        assert request2.claim_owner


def test_comment_semantic_failure_persists_evaluator_evidence() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _get_task(session, action)
        request = prepare_comment_generation_request(session, action, task)
        evidence = {
            "decision": "fail",
            "confidence": 0.96,
            "codes": ["context_mismatch"],
            "candidate_hash": "a" * 64,
        }

        _persist_generation_failure(
            session,
            request,
            "context_mismatch",
            code="quality_wait",
            evaluator_evidence=evidence,
            tokens=8,
        )

        refreshed_action = session.get(Action, action.id)
        job = _open_jobs(session, "comment-obligation-1")[0]
        assert refreshed_action.candidate_hash == "a" * 64
        assert refreshed_action.result["evaluator_evidence"] == evidence
        assert refreshed_action.payload["ai_generation_tokens"] == 8
        assert job.state == "failed"
        assert job.candidate_hash == "a" * 64
        assert job.evaluator_evidence == evidence


def _get_task(session: Session, action: Action):
    from app.models import Task

    return session.get(Task, action.task_id)


def _validated_payload(action: Action):
    from app.services.task_center.channel_payloads import PostCommentPayload

    return PostCommentPayload.model_validate(action.payload or {})


def test_finish_rejects_foreign_owner() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        payload = _validated_payload(action)
        claim_comment_generation_job(session, action, payload, owner="owner-a")

        with pytest.raises(CommentGenerationJobConflict):
            finish_comment_generation_job(session, action, payload, state="ready", owner="owner-b")

        job = _open_jobs(session, "comment-obligation-1")[0]
        assert job.state == "generating"
        assert job.generation_owner_id == "owner-a"


def test_claim_conflict_when_other_owner_holds_fresh_lease() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        payload = _validated_payload(action)
        claim_comment_generation_job(session, action, payload, owner="owner-a")

        with pytest.raises(CommentGenerationJobConflict):
            claim_comment_generation_job(session, action, payload, owner="owner-b")
