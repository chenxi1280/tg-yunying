"""Due, currently usable work must survive LIMIT ahead of missed windows."""
from datetime import timedelta

import pytest

from app.models import AccountBehaviorSessionPlan, AccountPacingReservation, Action
from app.services.task_center.direct_action_claims import _candidate_rows
from tests.test_due_backlog_containment import NOW, _seed_task_and_action, session

pytestmark = pytest.mark.no_postgres
BACKLOG_SIZE = 400


def _seed_window(session, account_id, *, hours):
    session.add(AccountBehaviorSessionPlan(
        tenant_id=1, account_id=account_id, task_day=NOW.date(),
        policy_revision_id="policy", chronotype="day", weekday_class="workday",
        seed="seed", windows=[{
            "start_at": (NOW + timedelta(hours=hours)).replace(tzinfo=None).isoformat(),
            "end_at": (NOW + timedelta(hours=hours, minutes=30)).replace(tzinfo=None).isoformat(),
        }],
    ))


def _candidates(session, limit=1):
    return [row[0] for row in _candidate_rows(session, limit=limit, now=NOW,
        exclude_task_ids=None, execution_lane="non_search")]


def test_current_session_is_selected_before_hundreds_of_old_candidates(session):
    task, _, _ = _seed_task_and_action(session, action_id="old-first",
        scheduled_at=NOW-timedelta(hours=2), deadline_at=NOW+timedelta(hours=10),
        message_text="ready", generation_status="ready")
    _seed_window(session, 101, hours=-2)
    _seed_window(session, 102, hours=0)
    for index in range(BACKLOG_SIZE):
        identifier = f"queued-{index}"
        session.add(Action(id=identifier, tenant_id=1, task_id=task.id,
            task_type=task.type, action_type="send_message", status="pending",
            account_id=101, scheduled_at=NOW-timedelta(hours=1),
            pacing_slot_key=identifier, payload={"message_text": "ready"}))
        session.add(AccountPacingReservation(tenant_id=1, task_id=task.id,
            action_id=identifier, account_id=101, pacing_slot_key=identifier,
            action_class="send_message", due_at=NOW-timedelta(hours=1),
            release_not_before_at=NOW-timedelta(hours=1), effective_claim_at=NOW-timedelta(hours=1),
            source_deadline_at=NOW+timedelta(hours=10), state="bound",
            policy_version="account_soft_pacing_behavior_session_v1"))
    session.add(Action(id="usable-now", tenant_id=1, task_id=task.id,
        task_type=task.type, action_type="send_message", status="pending",
        account_id=102, scheduled_at=NOW, pacing_slot_key="usable-now",
        payload={"message_text": "ready"}))
    session.add(AccountPacingReservation(tenant_id=1, task_id=task.id,
        action_id="usable-now", account_id=102, pacing_slot_key="usable-now",
        action_class="send_message", due_at=NOW,
        release_not_before_at=NOW, effective_claim_at=NOW,
        source_deadline_at=NOW+timedelta(hours=10), state="bound",
        policy_version="account_soft_pacing_behavior_session_v1"))
    session.query(AccountPacingReservation).filter_by(action_id="old-first").update(
        {"policy_version": "account_soft_pacing_behavior_session_v1"})
    session.add(Action(id="old-admission", tenant_id=1, task_id=task.id,
        task_type=task.type, action_type="ensure_target_membership", status="pending",
        account_id=101, scheduled_at=NOW-timedelta(days=1), payload={}))
    session.commit()
    assert _candidates(session) == ["usable-now"]


def test_expired_task_does_not_precede_another_tasks_current_work(session):
    _seed_task_and_action(session, task_id="expired", action_id="expired",
        scheduled_at=NOW-timedelta(days=1), deadline_at=NOW-timedelta(hours=1))
    _seed_task_and_action(session, task_id="current", action_id="current",
        scheduled_at=NOW, deadline_at=NOW+timedelta(hours=1), message_text="ready")
    assert _candidates(session) == ["current"]
    assert _candidates(session, limit=2) == ["current", "expired"]
