from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    SearchClickFulfillmentObligation,
    Task,
    TaskDayLedger,
    Tenant,
)
from app.services.task_center.dispatcher import (
    _settle_pure_search_click_obligation,
)


pytestmark = pytest.mark.no_postgres


def _complete_result() -> dict:
    return {
        "success": True,
        "target_click_observed": True,
        "membership_side_effect": "none",
        "membership_mutating_rpc_invoked": False,
        "target_username": "target",
        "bot_username": "jisou",
        "keyword_hash": "a" * 64,
        "target_message_id": "101",
        "target_position": 1,
        "target_button_row": 0,
        "target_button_col": 0,
        "target_button_type": "telegram_url",
        "target_button_effect": "target_open_only",
        "target_button_fingerprint": "fingerprint",
        "target_click_observed_at": "2026-07-29T10:00:00",
    }


def _facts(session: Session, *, executed_at: datetime):
    session.add(Tenant(id=1, name="单用户"))
    target = OperationTarget(
        tenant_id=1,
        target_type="group",
        tg_peer_id="target",
        title="目标",
    )
    task = Task(
        tenant_id=1,
        name="纯搜索点击",
        type="search_click",
        status="running",
    )
    session.add_all((target, task))
    session.flush()
    start = datetime(2026, 7, 29)
    ledger = TaskDayLedger(
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=start,
        deadline_at=start + timedelta(days=1),
        day_phase="full_day_committed",
        planning_anchor_at=start,
    )
    session.add(ledger)
    session.flush()
    obligation = SearchClickFulfillmentObligation(
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=target.id,
        click_obligation_ordinal=1,
    )
    session.add(obligation)
    session.flush()
    action = Action(
        tenant_id=1,
        task_id=task.id,
        task_type="search_click",
        action_type="search_join",
        status="success",
        payload={"search_click_obligation_id": obligation.id},
        result=_complete_result(),
        executed_at=executed_at,
    )
    session.add(action)
    session.flush()
    obligation.source_action_id = action.id
    attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        gateway_call_started_at=executed_at,
        status="after_call",
    )
    session.add(attempt)
    session.flush()
    return obligation, action, attempt


@pytest.mark.parametrize("offset,confirmed", [(timedelta(hours=10), True), (timedelta(days=1), False)])
def test_click_fact_only_settles_inside_frozen_ledger_period(
    offset: timedelta,
    confirmed: bool,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        obligation, action, attempt = _facts(
            session,
            executed_at=datetime(2026, 7, 29) + offset,
        )

        _settle_pure_search_click_obligation(session, action, attempt)

        assert (obligation.status == "confirmed") is confirmed
        assert obligation.target_click_observed is confirmed
        if confirmed:
            assert obligation.execution_attempt_id == attempt.id
            assert obligation.click_evidence_hash
        else:
            assert action.result["error_code"] == "click_fact_outside_ledger_period"
