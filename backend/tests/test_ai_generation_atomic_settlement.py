"""A prepared Action and its GenerationJob must publish as one transaction."""
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Action, GenerationJob, Task
from app.services.task_center import ai_generation_job_finish
from app.services.task_center.ai_generation_parallel_settlement import settle_parallel_outcome
from app.services.task_center.ai_generation_recovery import reconcile_generation_jobs
from app.services.task_center.ai_generation_worker_types import GenerationOutcome
from tests.ai_generation_settlement_support import (
    ACTION_ID, CONTENT, JOB_ID, OWNER, TASK_ID, seed_prepared_window, seed_ready_claim,
)


pytestmark = [pytest.mark.no_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def test_finish_failure_does_not_publish_action_without_ready_job(factory, monkeypatch):
    claim = seed_ready_claim(factory)

    def unavailable(*_args):
        raise RuntimeError("test_task_row_lock_busy")

    monkeypatch.setattr(ai_generation_job_finish, "_require_not_retired", unavailable)
    with pytest.raises(RuntimeError, match="test_task_row_lock_busy"):
        settle_parallel_outcome(factory, claim, GenerationOutcome())
    with factory() as session:
        action, job = session.get(Action, ACTION_ID), session.get(GenerationJob, JOB_ID)
        assert action.status == "executing"
        assert action.claim_owner == OWNER and action.claim_token == claim.token
        assert action.payload["message_text"] == CONTENT
        assert job.state == "generating" and job.job_version == claim.job_version


@pytest.mark.parametrize("changed", ["owner", "epoch", "token", "candidate"])
def test_stale_completion_keeps_current_action_claim(factory, changed):
    claim = seed_ready_claim(factory)
    with factory() as session:
        action, job = session.get(Action, ACTION_ID), session.get(GenerationJob, JOB_ID)
        if changed == "owner":
            job.generation_owner_id = "new-worker"
        elif changed == "epoch":
            job.generation_lease_epoch += 1
        elif changed == "token":
            claim = replace(claim, token="stale-token")
        else:
            action.candidate_hash = "incorrect"
        session.commit()
    with pytest.raises(RuntimeError):
        settle_parallel_outcome(factory, claim, GenerationOutcome())
    with factory() as session:
        assert session.get(Action, ACTION_ID).status == "executing"
        assert session.get(GenerationJob, JOB_ID).state == "generating"


def test_successful_completion_is_atomic_and_idempotent(factory):
    claim = seed_ready_claim(factory)
    assert settle_parallel_outcome(factory, claim, GenerationOutcome()) == 1
    assert settle_parallel_outcome(factory, claim, GenerationOutcome()) == 0
    with pytest.raises(RuntimeError):
        settle_parallel_outcome(factory, replace(claim, token="wrong-token"), GenerationOutcome())
    with factory() as session:
        action, job = session.get(Action, ACTION_ID), session.get(GenerationJob, JOB_ID)
        assert action.status == "pending" and not action.claim_owner
        assert job.state == "ready" and not job.generation_owner_id
        assert job.candidate_hash == action.candidate_hash


@pytest.mark.parametrize("status", ["executing", "pending"])
def test_expired_prepared_generation_preserves_original_candidate(factory, status):
    seed_ready_claim(factory, action_status=status)
    with factory() as session:
        action = session.get(Action, ACTION_ID)
        before = (action.candidate_hash, action.scheduled_at, action.obligation_id)
        assert reconcile_generation_jobs(session, limit=1) == 1
        session.commit()
        job = session.get(GenerationJob, JOB_ID)
        assert job.state == "ready" and job.generation_stage == "prepared_result_recovered"
        assert job.candidate_hash == action.candidate_hash
        assert job.evaluator_evidence["prepared_recovery"]["action_id"] == action.id
        assert action.status == "pending" and not action.claim_owner
        assert action.payload["message_text"] == CONTENT
        assert (action.candidate_hash, action.scheduled_at, action.obligation_id) == before
        assert reconcile_generation_jobs(session, limit=1) == 0


def test_expired_prepared_generation_rejects_hash_mismatch(factory):
    seed_ready_claim(factory)
    with factory() as session:
        session.get(Action, ACTION_ID).candidate_hash = "incorrect"
        session.commit()
        with pytest.raises(RuntimeError, match="ready_action_invalid"):
            reconcile_generation_jobs(session, limit=1)
        session.rollback()
        assert session.get(GenerationJob, JOB_ID).state == "generating"


def test_completion_cannot_publish_after_task_pause(factory):
    claim = seed_ready_claim(factory)
    with factory() as session:
        session.get(Task, TASK_ID).status = "paused"
        session.commit()
    with pytest.raises(RuntimeError, match="task"):
        settle_parallel_outcome(factory, claim, GenerationOutcome())
    with factory() as session:
        assert session.get(Action, ACTION_ID).status == "executing"


def test_settlement_only_releases_the_claimed_action(factory):
    claim = seed_ready_claim(factory)
    with factory() as session:
        original = session.get(Action, ACTION_ID)
        sibling = Action(id="other-owned-action", tenant_id=1, task_id=TASK_ID,
            task_type="group_ai_chat", action_type="send_message", account_id=11,
            status="executing", claim_owner=claim.owner, claim_token=claim.token,
            lease_owner=claim.owner, payload=dict(original.payload), candidate_hash=original.candidate_hash)
        session.add(sibling)
        session.commit()
    assert settle_parallel_outcome(factory, claim, GenerationOutcome()) == 1
    with factory() as session:
        assert session.get(Action, "other-owned-action").status == "executing"


def test_expired_ready_action_with_gateway_call_remains_unknown(factory):
    from app.models import ExecutionAttempt
    from app.services._common import _now

    seed_ready_claim(factory, action_status="pending")
    with factory() as session:
        session.add(ExecutionAttempt(action_id=ACTION_ID, tenant_id=1, account_id=11,
            status="result_unknown", gateway_call_started_at=_now()))
        session.commit()
        assert reconcile_generation_jobs(session, limit=1) == 1
        session.commit()
        job = session.get(GenerationJob, JOB_ID)
        assert job.state == "unknown" and job.generation_stage == "gateway_reconcile_required"
        assert session.get(Action, ACTION_ID).payload["message_text"] == CONTENT


def test_expired_ready_recovery_keeps_original_window_binding(factory):
    from app.models import AiContentWindowPlanSlot

    seed_ready_claim(factory)
    seed_prepared_window(factory)
    with factory() as session:
        slot = session.get(AiContentWindowPlanSlot, "settlement-window")
        before = (slot.id, slot.version, slot.due_at, slot.claimed_by_job_id)
        assert reconcile_generation_jobs(session, limit=1) == 1
        session.commit()
        session.refresh(slot)
        assert session.get(GenerationJob, JOB_ID).state == "ready"
        assert slot.state == "candidate_ready"
        assert (slot.id, slot.version, slot.due_at, slot.claimed_by_job_id) == before


@pytest.mark.parametrize("entry", ["settle", "reconcile"])
def test_invalidated_window_does_not_regain_publication_rights(factory, entry):
    from app.models import AiContentWindowPlanSlot

    claim = seed_ready_claim(factory)
    seed_prepared_window(factory, state="invalidated")
    with pytest.raises(RuntimeError, match="ready_window_invalid"):
        if entry == "settle":
            settle_parallel_outcome(factory, claim, GenerationOutcome())
        else:
            with factory() as session:
                reconcile_generation_jobs(session, limit=1)
    with factory() as check:
        assert check.get(GenerationJob, JOB_ID).state == "generating"
        assert check.get(Action, ACTION_ID).status == "executing"
        assert check.get(AiContentWindowPlanSlot, "settlement-window").state == "invalidated"
