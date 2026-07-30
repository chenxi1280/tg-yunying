from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiCoverageVariationIntent,
    AuditLog,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.ai_backlog_abandonment import (
    abandon_ai_historical_backlog,
)

pytestmark = pytest.mark.no_postgres


def test_preview_does_not_change_ai_backlog() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    cutoff = datetime(2026, 7, 30, 18, 0)

    with Session(engine) as session:
        facts = _seed_backlog(session, cutoff)
        result = abandon_ai_historical_backlog(
            session,
            cutoff=cutoff,
            apply=False,
            actor="test",
        )

        assert result["candidate_count"] == 1
        assert session.get(Action, facts["action_id"]) is not None
        assert session.get(TaskGroupDailyMessageSlot, facts["quantity_id"]).state == "open"
        assert session.get(TaskAccountDailyCoverage, facts["coverage_id"]).state == "reserved"


def test_apply_terminalizes_business_slots_before_deleting_action() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    cutoff = datetime(2026, 7, 30, 18, 0)

    with Session(engine) as session:
        facts = _seed_backlog(session, cutoff)
        result = abandon_ai_historical_backlog(
            session,
            cutoff=cutoff,
            apply=True,
            actor="test",
        )
        session.commit()

        assert result["deleted_action_count"] == 1
        assert session.get(Action, facts["action_id"]) is None
        assert session.get(Action, facts["future_action_id"]) is not None
        assert session.get(Action, facts["gateway_action_id"]) is not None
        assert session.get(TaskGroupDailyMessageSlot, facts["quantity_id"]).state == "terminal"
        coverage = session.get(TaskAccountDailyCoverage, facts["coverage_id"])
        assert coverage.state == "abandoned"
        assert coverage.reserved_action_id is None
        intent = session.scalar(select(AiCoverageVariationIntent))
        assert intent and intent.action_id is None
        assert intent.outcome == "operator_abandoned_historical_backlog"
        assert session.scalar(select(AuditLog)) is not None

        repeated = abandon_ai_historical_backlog(
            session,
            cutoff=cutoff,
            apply=True,
            actor="test",
        )
        assert repeated["candidate_count"] == 0


def test_task_filter_limits_abandonment_scope() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    cutoff = datetime(2026, 7, 30, 18, 0)

    with Session(engine) as session:
        facts = _seed_backlog(session, cutoff)
        result = abandon_ai_historical_backlog(
            session,
            cutoff=cutoff,
            apply=True,
            actor="test",
            task_ids={"different-task"},
        )

        assert result["candidate_count"] == 0
        assert session.get(Action, facts["action_id"]) is not None


def _seed_backlog(session: Session, cutoff: datetime) -> dict[str, str]:
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(id="ai-task", tenant_id=1, name="AI", type="group_ai_chat", status="running"))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="***",
        status="active",
    ))
    session.add(TgGroup(id=21, tenant_id=1, title="group", tg_peer_id="-10021"))
    ledger = TaskDayLedger(
        id="ledger",
        tenant_id=1,
        task_id="ai-task",
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=cutoff.date(),
        period_start_at=cutoff - timedelta(hours=18),
        deadline_at=cutoff + timedelta(hours=6),
        day_phase="full_day_committed",
        planning_anchor_at=cutoff - timedelta(hours=18),
    )
    session.add(ledger)
    coverage = TaskAccountDailyCoverage(
        id="coverage",
        tenant_id=1,
        task_id="ai-task",
        task_day_ledger_id=ledger.id,
        group_id=21,
        account_id=11,
        coverage_date=cutoff.date(),
        state="reserved",
    )
    quantity = TaskGroupDailyMessageSlot(
        id="quantity",
        tenant_id=1,
        task_id="ai-task",
        task_day_ledger_id=ledger.id,
        target_operation_target_id=1,
        task_account_daily_coverage_id=coverage.id,
        slot_kind="account_coverage",
        slot_ordinal=1,
    )
    session.add_all([coverage, quantity])
    session.flush()
    overdue = _action("overdue", cutoff - timedelta(minutes=5), quantity.id)
    future = _action("future", cutoff + timedelta(minutes=5), None)
    gateway = _action("gateway", cutoff - timedelta(minutes=4), None)
    gateway.result = {"gateway_call_started_at": cutoff.isoformat()}
    session.add_all([overdue, future, gateway])
    session.flush()
    coverage.reserved_action_id = overdue.id
    coverage.last_action_id = overdue.id
    session.add(AiCoverageVariationIntent(
        tenant_id=1,
        coverage_ledger_id=coverage.id,
        action_id=overdue.id,
        content_variation_key="variation",
    ))
    session.commit()
    return {
        "action_id": overdue.id,
        "future_action_id": future.id,
        "gateway_action_id": gateway.id,
        "quantity_id": quantity.id,
        "coverage_id": coverage.id,
    }


def _action(action_id: str, scheduled_at: datetime, quantity_id: str | None) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="ai-task",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=scheduled_at,
        primary_quantity_slot_id=quantity_id,
        payload={"ai_generation_status": "pending"},
        result={},
    )
