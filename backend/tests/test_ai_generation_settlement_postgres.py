"""Real Task row-lock contention and generation-stage database constraints."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import Action, GenerationJob, Task
from app.services.task_center.ai_generation_parallel_settlement import settle_parallel_outcome
from app.services.task_center.ai_generation_worker_types import GenerationOutcome
from app.services.task_center.ai_group_emergency_pending import mark_emergency_pending
from tests.ai_generation_settlement_support import (
    ACTION_ID, JOB_ID, OWNER, TASK_ID, seed_prepared_window, seed_ready_claim,
)
from tests.postgres_pacing_e4_fixture import factory as factory
from tests.test_ai_group_emergency_postgres import _seed


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def test_task_row_lock_failure_rolls_back_action_publication(factory):
    claim = seed_ready_claim(factory)
    with factory() as planner:
        planner.scalar(select(Task).where(Task.id == TASK_ID).with_for_update())
        with pytest.raises(OperationalError, match="could not obtain lock"):
            settle_parallel_outcome(factory, claim, GenerationOutcome())
        with factory() as check:
            assert check.get(Action, ACTION_ID).status == "executing"
            assert check.get(Action, ACTION_ID).claim_owner == OWNER
            assert check.get(GenerationJob, JOB_ID).state == "generating"
        planner.rollback()
    assert settle_parallel_outcome(factory, claim, GenerationOutcome()) == 1


def test_long_emergency_reason_keeps_full_evidence_without_stage_overflow(factory):
    reason = "topic_only_topic_evidence_missing"
    with factory() as session:
        _seed(session)
        action, job = session.get(Action, "action"), session.get(GenerationJob, "job")
        action.status, action.claim_owner, action.claim_token = "executing", OWNER, "token"
        action.payload = {**action.payload, "ai_generation_status": "generating"}
        job.state, job.generation_owner_id = "generating", OWNER
        session.commit()
        assert mark_emergency_pending(session, SimpleNamespace(claim_owner=OWNER, attempt_id="old-attempt"),
                                      action=action, reason=reason)
        session.commit()
    with factory() as check:
        job, action = check.get(GenerationJob, "job"), check.get(Action, "action")
        assert job.state == "failed" and job.generation_stage == "emergency_pending"
        assert job.evaluator_evidence["emergency_generation"]["reason"] == reason
        assert action.result["error_code"] == reason
        assert action.payload["ai_generation_status"] == "emergency_pending"


def test_recovery_cannot_take_over_a_live_locked_prepared_action(factory):
    from app.services.task_center.ai_generation_recovery import reconcile_generation_jobs

    seed_ready_claim(factory)
    seed_prepared_window(factory)
    with factory() as publisher:
        publisher.scalar(select(Action).where(Action.id == ACTION_ID).with_for_update())
        with factory() as recovery:
            with pytest.raises(OperationalError, match="could not obtain lock"):
                reconcile_generation_jobs(recovery, limit=1)
            recovery.rollback()
        publisher.rollback()
    with factory() as recovery:
        assert recovery.get(GenerationJob, JOB_ID).generation_owner_id == OWNER
        assert reconcile_generation_jobs(recovery, limit=1) == 1
        recovery.commit()
    with factory() as check:
        job, action = check.get(GenerationJob, JOB_ID), check.get(Action, ACTION_ID)
        assert job.state == "ready" and action.status == "pending"
        assert job.candidate_hash == action.candidate_hash
