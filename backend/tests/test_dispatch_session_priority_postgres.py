from datetime import timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import AccountBehaviorBudgetPolicyRevision, AccountBehaviorSessionPlan, AccountPacingReservation, Action
from app.services.task_center.direct_action_claims import _candidate_rows
from tests.test_dispatch_session_priority import NOW
from tests.test_engagement_assignment_postgres import _seed
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _candidate(session, task, *, account_id, identity, hours):
    due = NOW + timedelta(hours=hours)
    session.add(AccountBehaviorSessionPlan(tenant_id=1, account_id=account_id,
        task_day=NOW.date(), policy_revision_id="policy", chronotype="day",
        weekday_class="workday", seed=identity, windows=[{
            "start_at": due.replace(tzinfo=None).isoformat(),
            "end_at": (due+timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
        }]))
    session.add(Action(id=identity, tenant_id=1, task_id=task.id, account_id=account_id,
        task_type=task.type, action_type="send_message", status="pending",
        scheduled_at=due, pacing_slot_key=identity, payload={"message_text": "QA-ready"}))
    session.flush()
    session.add(AccountPacingReservation(tenant_id=1, task_id=task.id,
        action_id=identity, account_id=account_id, pacing_slot_key=identity,
        action_class="authored_message", due_at=due, release_not_before_at=due,
        effective_claim_at=due, source_deadline_at=NOW+timedelta(hours=10),
        state="bound", policy_version="account_soft_pacing_behavior_session_v1"))


def test_actual_postgres_json_windows_and_utc_connection_select_current_work(database):
    with Session(database) as session:
        task = _seed(session)
        task.type = "group_ai_chat"
        task.fulfillment_contract_version = "fact_first_v3"
        session.add(AccountBehaviorBudgetPolicyRevision(id="policy", tenant_id=1))
        session.flush()
        _candidate(session, task, account_id=11, identity="old", hours=-2)
        _candidate(session, task, account_id=12, identity="current", hours=0)
        session.commit()
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        rows = _candidate_rows(session, limit=1, now=NOW.astimezone(timezone.utc),
            exclude_task_ids=None, execution_lane="non_search")
        assert [row[0] for row in rows] == ["current"]
        assert session.get(Action, "old").status == "pending"
