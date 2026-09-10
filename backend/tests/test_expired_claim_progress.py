"""Expired work progresses even while the live dispatch batch stays full."""
from datetime import timedelta

import pytest

from app.models import Action, ExecutionAttempt
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from tests.test_due_backlog_containment import NOW, _seed_task_and_action, session

pytestmark = pytest.mark.no_postgres


def seed_pair(session):
    _, expired, _ = _seed_task_and_action(session, task_id="expired-task",
        action_id="expired", scheduled_at=NOW-timedelta(days=1),
        deadline_at=NOW-timedelta(hours=1))
    _, live, _ = _seed_task_and_action(session, task_id="live-task",
        action_id="live", scheduled_at=NOW,
        deadline_at=NOW+timedelta(hours=1), message_text="ready")
    return expired, live


def claim(session, **options):
    return claim_fact_first_candidates(session, owner="progress-test", limit=1,
        now=NOW, lease_seconds=30, **options)


def test_expired_settlement_does_not_depend_on_live_batch_becoming_empty(session):
    expired, live = seed_pair(session)
    batch = claim(session)
    session.refresh(expired)
    assert expired.status == "skipped"
    assert expired.result["error_code"] == "pacing_claim_deadline_exceeded"
    assert batch.action_ids == (live.id,)


def test_excluded_task_is_not_settled(session):
    expired, live = seed_pair(session)
    batch = claim(session, exclude_task_ids={expired.task_id})
    session.refresh(expired)
    assert expired.status == "pending"
    assert batch.action_ids == (live.id,)


def test_other_lane_is_not_settled(session):
    expired, live = seed_pair(session)
    expired.execution_lane = "search"
    session.commit()
    batch = claim(session, execution_lane="non_search")
    session.refresh(expired)
    assert expired.status == "pending"
    assert batch.action_ids == (live.id,)


def test_remote_unknown_is_preserved_when_live_work_exists(session):
    expired, live = seed_pair(session)
    session.add(ExecutionAttempt(id="unknown-attempt", tenant_id=1,
        action_id=expired.id, account_id=101, attempt_no=1,
        status="result_unknown", gateway_call_started_at=NOW-timedelta(hours=2),
        failure_type="unknown_after_send"))
    session.commit()
    batch = claim(session)
    attempt = session.get(ExecutionAttempt, "unknown-attempt")
    assert attempt.status == "result_unknown"
    assert attempt.gateway_call_started_at is not None
    assert batch.action_ids == (live.id,)
