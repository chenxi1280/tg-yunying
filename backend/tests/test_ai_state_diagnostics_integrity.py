from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.models import Action, FulfillmentObligationProjection, GenerationJob
from app.timezone import BEIJING_TZ
from app.services.task_center.ai_generation_outcome_diagnostics import generation_outcome_diagnostics
from app.services.task_center.ai_queue_diagnostics import original_deadline_state
from test_production_e4_reporting_integrity import scope, _message

pytestmark = pytest.mark.no_postgres
ANCHOR = datetime(2026, 9, 10, 12, tzinfo=BEIJING_TZ)


def _work(session, key, *, status="pending", generation="", stage="emergency_pending", job_state="failed"):
    action = Action(id=f"action-{key}", tenant_id=1, task_id="task", task_type="group_ai_chat",
        action_type="send_message", status=status, obligation_type="quantity_slot", obligation_id=key,
        scheduled_at=ANCHOR, created_at=ANCHOR.replace(tzinfo=None),
        payload={"ai_generation_status": generation, "message_text": "neutral" if generation == "ready" else ""})
    job = GenerationJob(id=f"job-{key}", tenant_id=1, task_id="task", obligation_type="quantity_slot",
        obligation_id=key, generation_sequence=1, context_snapshot_version=1,
        state=job_state, generation_stage=stage, created_at=ANCHOR)
    session.add_all([action, job])
    session.flush()
    return action, job


def test_failed_normal_job_with_prepared_successor_is_not_generation_failure(scope):
    session, task, _ = scope
    _work(session, "prepared", generation="ready")
    _work(session, "emergency", generation="emergency_pending")
    action, _ = _work(session, "admission", status="skipped", stage="routing")
    action.result = {"error_code": "c2_account_abandoned"}
    _work(session, "receipt", status="success")
    _work(session, "telegram", status="unknown_after_send")
    _work(session, "provider", stage="provider_result_unknown", job_state="unknown")
    session.flush()
    result = generation_outcome_diagnostics(session, task)
    assert result["outcome_counts"] == {"content_ready": 1, "emergency_pending": 1,
        "admission_blocked": 1, "receipt_without_visible_fact": 1, "telegram_unknown": 1, "provider_unknown": 1}
    assert result["work_count"] == 6


def test_multiple_materializations_and_job_generations_count_one_current_owner(scope):
    session, task, _ = scope
    old, job = _work(session, "same-owner", status="failed")
    current = Action(id="new-action", tenant_id=1, task_id=task.id, task_type="group_ai_chat",
        action_type="send_message", status="pending", obligation_type=old.obligation_type,
        obligation_id=old.obligation_id, materialization_version=2,
        payload={"ai_generation_status": "ready", "message_text": "neutral"})
    session.add(current)
    session.add(GenerationJob(id="next-job", tenant_id=1, task_id=task.id,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id,
        generation_sequence=2, context_snapshot_version=2, state="ready", generation_stage="gateway_bound"))
    session.flush()
    result = generation_outcome_diagnostics(session, task)
    assert result["outcome_counts"] == {"content_ready": 1}
    assert result["latest_job_state_stage_counts"] == {"ready:gateway_bound": 1}
    session.add(FulfillmentObligationProjection(tenant_id=1, task_id=task.id,
        obligation_type=old.obligation_type, obligation_id=old.obligation_id,
        active_action_id=old.id, state="open", work_lane="interaction"))
    session.flush()
    assert generation_outcome_diagnostics(session, task)["outcome_counts"] == {"terminal_failed": 1}


def test_release_anchor_excludes_history_but_keeps_new_job_on_existing_action(scope):
    session, task, _ = scope
    action, job = _work(session, "old")
    action.created_at = (ANCHOR - timedelta(hours=1)).replace(tzinfo=None)
    job.created_at = ANCHOR - timedelta(hours=1)
    session.flush()
    assert generation_outcome_diagnostics(session, task, since=ANCHOR)["work_count"] == 0
    job.created_at = ANCHOR + timedelta(seconds=1)
    session.flush()
    result = generation_outcome_diagnostics(session, task, since=ANCHOR)
    assert result["work_count"] == 1 and result["scope"]["since"] == ANCHOR.isoformat()


def test_visible_completion_requires_matching_attempt_and_message_and_is_readonly(scope):
    session, task, _ = scope
    _, attempt, fact = _message(session)
    assert generation_outcome_diagnostics(session, task)["outcome_counts"] == {"remote_confirmed": 1}
    fact.outcome = {"remote_message_id": "wrong"}
    session.flush()
    statements = []
    event.listen(session.bind, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    result = generation_outcome_diagnostics(session, task)
    assert result["outcome_counts"] == {"receipt_without_visible_fact": 1}
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize("state,called,release,now,expected", [
    ("pending", False, ANCHOR, ANCHOR - timedelta(seconds=1), "outside_original_deadline"),
    ("pending", False, ANCHOR - timedelta(seconds=1), ANCHOR - timedelta(seconds=2), "valid_wait"),
    ("pending", False, ANCHOR, ANCHOR, "expired_uncalled"),
    ("pending", True, ANCHOR, ANCHOR, "called_history"),
    ("unknown_after_send", True, ANCHOR, ANCHOR, "unknown_preserved"),
    ("closed_unknown", True, ANCHOR, ANCHOR, "unknown_preserved"),
    ("success", True, ANCHOR, ANCHOR, "terminal"),
])
def test_original_deadline_does_not_turn_called_or_unknown_work_into_expired_unsent(state, called, release, now, expected):
    action = Action(status=state, scheduled_at=release, release_not_before_at=release)
    assert original_deadline_state(action, deadline=ANCHOR, called=called, now=now) == expected
    assert action.status == state and action.release_not_before_at == release
