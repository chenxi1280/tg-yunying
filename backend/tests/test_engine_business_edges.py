from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AccountBehaviorBudgetPolicyRevision, AccountPacingReservation, Action, GenerationJob
from app.services.task_center import generation_pending_timing as timing
from app.services.task_center.album_reaction_timing import available_album_children
from app.services.task_center.engagement_behavior_sessions import ensure_behavior_session_plan
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def _job_action(session, task, *, state="pending", retry=None, request_hash=""):
    job = GenerationJob(id="job", tenant_id=1, task_id=task.id, task_lifecycle_epoch=1,
        obligation_type="send_message", obligation_id="quantity-1", generation_sequence=1,
        context_snapshot_version=1, state=state, generation_not_before_at=NOW+timedelta(seconds=5),
        next_retry_at=retry, request_hash=request_hash)
    action = Action(id="action", tenant_id=1, task_id=task.id, task_type=task.type,
        task_lifecycle_epoch=1, action_type="send_message", account_id=1, status="pending",
        obligation_type="send_message", obligation_id="quantity-1", scheduled_at=NOW+timedelta(seconds=5),
        payload={"generation_job_id": job.id})
    session.add_all([job, action])
    session.flush()
    return job, action


def test_pristine_old_pending_job_gets_jit_not_before_without_changing_send(monkeypatch):
    monkeypatch.setattr(timing, "_now", lambda: NOW)
    session, task, _, _ = seed_source_session(task_type="group_ai_chat")
    with session:
        job, action = _job_action(session, task)
        timing.refresh_pending_generation_timing(session, task_type=task.type, limit=20)
        assert job.generation_not_before_at == NOW-timedelta(seconds=5)
        assert action.scheduled_at == NOW+timedelta(seconds=5)
        assert job.job_version == 2


@pytest.mark.parametrize("change", ["other_lane", "future_action", "unknown", "request_started", "retry"])
def test_jit_repair_does_not_cross_lane_or_reset_started_unknown_retry(monkeypatch, change):
    monkeypatch.setattr(timing, "_now", lambda: NOW)
    session, task, _, _ = seed_source_session(task_type="group_ai_chat")
    with session:
        job, action = _job_action(session, task,
            state="unknown" if change == "unknown" else "pending",
            request_hash="started" if change == "request_started" else "",
            retry=NOW+timedelta(seconds=10) if change == "retry" else None)
        if change == "future_action":
            action.effective_claim_at = NOW+timedelta(hours=1)
        timing.refresh_pending_generation_timing(session,
            task_type="channel_comment" if change == "other_lane" else task.type, limit=20)
        assert job.generation_not_before_at == NOW+timedelta(seconds=5)
        assert job.job_version == 1


def test_album_preview_uses_real_timeline_and_leaves_no_reservations():
    session, task, _, _ = seed_source_session(accounts=1)
    with session:
        children = [message(session, i, album="album") for i in (1, 2)]
        session.add(AccountBehaviorBudgetPolicyRevision(tenant_id=1, account_class="normal",
            action_budgets={"reaction": 20}, session_budget={}, pair_gap_policy={}, wake_budget=0))
        session.flush()
        plan = ensure_behavior_session_plan(session, tenant_id=1, account_id=1, task_day=NOW.date())
        plan.windows = [{"start_at": (NOW-timedelta(minutes=1)).isoformat(),
                         "end_at": (NOW+timedelta(hours=1)).isoformat()}]
        session.flush()
        assert available_album_children(session, task, account_id=1, children=children,
            due_at=NOW, deadline_at=NOW+timedelta(seconds=1)) == 1
        assert list(session.scalars(select(AccountPacingReservation))) == []
