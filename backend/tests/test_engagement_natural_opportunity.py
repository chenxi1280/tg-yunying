from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ManagedPresencePlan,
    ManagedPresencePolicyRevision,
    NaturalOpportunitySupplyPlanRevision,
    Task,
    TaskDayLedger,
    Tenant,
    TgGroup,
)
from app.services.task_center.engagement_natural_opportunity import (
    ensure_natural_opportunity_plan,
)


pytestmark = pytest.mark.no_postgres
DAY_START = datetime(2026, 9, 3, 16, tzinfo=timezone.utc)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="测试群"))
    session.add(ManagedPresencePolicyRevision(tenant_id=1))
    session.add(Task(
        id="group-presence", tenant_id=1, name="活群", type="group_ai_chat",
        status="running",
        type_config={
            "engagement_contract_version": "unified_engagement_v1",
            "target_group_id": 21,
        },
        stats={},
    ))
    session.flush()
    session.add(TaskDayLedger(
        id="group-presence-day", tenant_id=1, task_id="group-presence",
        timezone_snapshot="Asia/Shanghai", timezone_revision=1,
        obligation_local_date=date(2026, 9, 4),
        period_start_at=DAY_START,
        deadline_at=DAY_START + timedelta(days=1),
        day_phase="full_day", planning_anchor_at=DAY_START,
    ))
    session.commit()
    return session


def _pending_action(action_id: str, minute: int) -> Action:
    return Action(
        id=action_id, tenant_id=1, task_id="group-presence",
        task_type="group_ai_chat", action_type="send_message", account_id=None,
        status="pending", scheduled_at=DAY_START + timedelta(minutes=minute),
        payload={"group_id": 21, "message_text": "待发送"},
    )


def test_cold_group_only_guarantees_bootstrap_and_consecutive_headroom() -> None:
    with _session() as session:
        task = session.get(Task, "group-presence")
        ledger = session.get(TaskDayLedger, "group-presence-day")
        group = session.get(TgGroup, 21)

        decision = ensure_natural_opportunity_plan(
            session, task, ledger, group=group, required_units=3,
        )

        assert decision.guaranteed_now_capacity == 2
        assert decision.deficit == 1
        assert decision.plan.commitment_status == "opportunity_unproven"
        assert task.last_error == "natural_opportunity_plan_unproven"


def test_open_actions_consume_presence_before_gateway_and_append_successor() -> None:
    with _session() as session:
        task = session.get(Task, "group-presence")
        ledger = session.get(TaskDayLedger, "group-presence-day")
        group = session.get(TgGroup, 21)
        first = ensure_natural_opportunity_plan(
            session, task, ledger, group=group, required_units=2,
        )
        session.add(_pending_action("presence-pending-1", 1))
        session.flush()

        second = ensure_natural_opportunity_plan(
            session, task, ledger, group=group, required_units=2,
        )

        assert first.plan.state == "superseded"
        assert second.plan.plan_revision == 2
        assert second.guaranteed_now_capacity == 1
        assert second.presence.planned_managed_authored_count == 1
        assert second.presence.trailing_managed_turn_count == 1
        assert len(session.scalars(
            select(NaturalOpportunitySupplyPlanRevision)
        ).all()) == 2
        assert len(session.scalars(select(ManagedPresencePlan)).all()) == 1
