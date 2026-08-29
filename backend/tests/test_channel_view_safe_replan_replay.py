from datetime import datetime

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.no_postgres

from app.models import (
    Action,
    ChannelViewDailyIdentityOwner,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
)
from app.services.task_center.direct_action_claims import (
    settle_fact_first_action_before_gateway,
)
from app.services.task_center.executors.channel_view import build_plan
from app.timezone import BEIJING_TZ
from tests.channel_view_coverage_support import new_session
from tests.test_channel_view_daily_identity_lifecycle import (
    _bind_pacing_reservation,
    _seed_two_tasks,
    _set_view_clock,
    _view_obligation,
)


def _bind_replacement(session, action, reservation, *, current):
    task = session.get(Task, action.task_id)
    assert task is not None
    obligation = _view_obligation(session, task)
    replacement = Action(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        task_type=action.task_type,
        action_type=action.action_type,
        account_id=action.account_id,
        scheduled_at=current,
        status="pending",
        payload=dict(action.payload or {}),
        obligation_type=action.obligation_type,
        obligation_id=action.obligation_id,
    )
    session.add(replacement)
    session.flush()
    obligation.status = "pending"
    obligation.current_action_id = replacement.id
    daily_owner = session.scalar(select(ChannelViewDailyIdentityOwner).where(
        ChannelViewDailyIdentityOwner.obligation_local_date == current.date(),
    ))
    assert daily_owner is not None
    daily_owner.state = "pre_gateway"
    daily_owner.obligation_id = obligation.id
    daily_owner.action_id = replacement.id
    reservation.state = "bound"
    reservation.action_id = replacement.id
    session.flush()
    return replacement


def test_safe_replan_settlement_replay_preserves_rebound_resources(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        task, _second = _seed_two_tasks(session, channel_id=86, current=current)
        assert build_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        assert action is not None
        reservation = _bind_pacing_reservation(session, action, current=current)
        action.status = "failed"
        settle_fact_first_action_before_gateway(
            session, action, now=current, reason_code="temporary_failure",
            detail="可安全重排", replan_same_obligation=True,
        )
        session.commit()
        attempt_count = session.query(ExecutionAttempt).filter_by(action_id=action.id).count()
        fact_count = session.query(FulfillmentRemoteFact).filter_by(action_id=action.id).count()

        assert settle_fact_first_action_before_gateway(
            session, action, now=current, reason_code="temporary_failure",
            detail="可安全重排", replan_same_obligation=True,
        ) == set()
        assert reservation.state == "reserved" and reservation.action_id is None
        replacement = _bind_replacement(session, action, reservation, current=current)

        assert settle_fact_first_action_before_gateway(
            session, action, now=current, reason_code="temporary_failure",
            detail="replacement 已接管后的旧 settlement 重放",
            replan_same_obligation=True,
        ) == set()
        assert reservation.state == "bound" and reservation.action_id == replacement.id
        assert session.query(ExecutionAttempt).filter_by(action_id=action.id).count() == attempt_count
        assert session.query(FulfillmentRemoteFact).filter_by(action_id=action.id).count() == fact_count
