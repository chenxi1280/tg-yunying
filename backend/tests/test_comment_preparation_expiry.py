"""Expired preparation is a terminal local outcome, never a provider call."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPacingReservation, CommentFulfillmentObligation, GenerationJob
from app.services._common import _now
from app.timezone import as_beijing
from app.services.task_center import comment_generation_dispatch as dispatch
from app.services.task_center import comment_generation_worker as worker
from app.services.task_center.ai_generation_claim_lifecycle import mark_generation_claim
from app.services.task_center.generation_timing_binding import _deadline_snapshot
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope

pytestmark = pytest.mark.no_postgres


def _expired_preparation(session, task, action, *, payload, job):
    return _deadline_snapshot(SimpleNamespace(), deadline=as_beijing(job.latest_safe_send_at),
                              now_value=as_beijing(_now()))


def _prepare_expired(session):
    action = seed_dispatch_scope(session)
    deadline = _now() - timedelta(seconds=1)
    session.add(AccountPacingReservation(tenant_id=1, task_id=action.task_id,
        account_id=action.account_id, pacing_slot_key="neutral-expired-comment",
        policy_version="soft_pacing_v1", due_at=deadline, release_not_before_at=deadline,
        effective_claim_at=deadline, source_deadline_at=deadline, action_id=action.id, state="bound"))
    claim = worker.CommentGenerationClaim(action.id, "neutral-owner", "neutral-token")
    mark_generation_claim(action, claim.owner, claim.token)
    session.commit()
    return action, claim


def test_expired_preparation_settles_without_requeue_or_provider(monkeypatch):
    monkeypatch.setattr(dispatch, "_generation_config", _expired_preparation)
    def no_provider(*args, **kwargs):
        pytest.fail("expired preparation reached provider")
    monkeypatch.setattr(dispatch, "_generate_comment", no_provider)
    with comment_dispatch_session() as session:
        action, claim = _prepare_expired(session)
        factory = lambda: Session(session.get_bind())
        worker._process_comment_generation(factory, claim,
            dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.expire_all()
        assert action.status == "failed"
        assert action.result["error_code"] == "generation_timing_preparation_deadline_missed"
        assert action.result["generation_outcome"] == "shortfall"
        assert action.claim_owner == action.claim_token == ""
        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        assert obligation.status == "terminal_shortfall"
        assert session.scalar(select(GenerationJob)).state == "failed"
        assert session.scalar(select(AccountPacingReservation)).state == "missed"
        assert worker._claim_comment_generation(factory, owner="next", excluded_action_ids=set()) is None


@pytest.mark.parametrize("status", ["confirmed", "unknown"])
def test_expiry_cannot_overwrite_terminal_or_unknown_obligation(monkeypatch, status):
    monkeypatch.setattr(dispatch, "_generation_config", _expired_preparation)
    with comment_dispatch_session() as session:
        action, claim = _prepare_expired(session)
        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        obligation.status = status
        session.commit()
        with pytest.raises(RuntimeError, match="comment_obligation_not_pending"):
            worker._process_comment_generation(lambda: Session(session.get_bind()), claim,
                dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.expire_all()
        assert obligation.status == status
        assert session.scalar(select(AccountPacingReservation)).state == "bound"


@pytest.mark.parametrize("boundary", ["provider", "gateway", "owner"])
def test_expiry_refuses_unresolved_call_or_other_owner(boundary):
    from app.models import ExecutionAttempt, Task
    from app.services.task_center.channel_payloads import PostCommentPayload
    from app.services.task_center.comment_generation_expiry import settle_expired_preparation
    from app.services.task_center.comment_generation_job import claim_comment_generation_job
    with comment_dispatch_session() as session:
        action, claim = _prepare_expired(session)
        job = claim_comment_generation_job(session, action,
            PostCommentPayload.model_validate(action.payload), owner=claim.owner)
        reason = "owner_mismatch"
        if boundary == "provider":
            job.state = "unknown"
            reason = "provider_unresolved"
        if boundary == "gateway":
            session.add(ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=action.account_id,
                attempt_no=1, status="unknown", gateway_call_started_at=_now()))
            reason = "remote_evidence_unsafe"
        if boundary == "owner":
            job.generation_owner_id = "another-owner"
        session.flush()
        with pytest.raises(RuntimeError, match=reason):
            settle_expired_preparation(session, action, task=session.get(Task, action.task_id), job=job)
        assert action.status == "executing"
        assert session.scalar(select(AccountPacingReservation)).state == "bound"


@pytest.mark.parametrize("field,value,reason", [
    ("tenant_id", 2, "scope_mismatch"),
    ("task_lifecycle_epoch", 2, "epoch_mismatch"),
])
def test_expiry_rejects_mismatched_obligation_identity(monkeypatch, field, value, reason):
    monkeypatch.setattr(dispatch, "_generation_config", _expired_preparation)
    with comment_dispatch_session() as session:
        action, claim = _prepare_expired(session)
        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        setattr(obligation, field, value)
        session.commit()
        with pytest.raises(RuntimeError, match=reason):
            worker._process_comment_generation(lambda: Session(session.get_bind()), claim,
                dependencies=worker.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.expire_all()
        assert obligation.status == "pending"
        assert session.scalar(select(AccountPacingReservation)).state == "bound"
