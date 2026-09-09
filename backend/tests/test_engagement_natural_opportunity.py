from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

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
from app.services.task_center.engagement_runtime_policy import _ensure_presence_policy
from app.services.task_center.executors import group_ai_chat


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


@pytest.mark.parametrize("idle_continuation", [False, True])
def test_exhausted_presence_does_not_block_committed_active_topic_supply(monkeypatch, idle_continuation):
    with _session() as session:
        task = session.get(Task, "group-presence")
        task.type_config = {**task.type_config, "idle_continuation_enabled": idle_continuation}
        ledger = session.get(TaskDayLedger, "group-presence-day")
        group = session.get(TgGroup, 21)
        session.add_all([_pending_action("occupied-1", 1), _pending_action("occupied-2", 2)])
        session.flush()
        candidate = SimpleNamespace(account_id=1)
        monkeypatch.setattr(group_ai_chat, "_ready_coverage_rows_for_plan",
            lambda *_args, **_kwargs: [candidate])
        monkeypatch.setattr(group_ai_chat, "_portfolio_coverage_rows",
            lambda *_args, **kwargs: kwargs["rows"])

        rows = group_ai_chat._coverage_candidate_rows(
            session, task, group=group, ledger=ledger, target=None,
            participation=SimpleNamespace(), admission=None,
            timestamp=DAY_START, required_units=1,
        )

        assert rows == [candidate]
        presence = session.scalar(select(ManagedPresencePlan))
        assert presence.remaining_capacity == 0
        assert presence.planned_managed_authored_count == 2
        assert task.last_error == ""
        assert task.stats["natural_opportunity"]["effect"] == "quality_observation_only"
        assert task.stats["natural_opportunity"]["deficit"] == 1


def test_new_presence_policy_preserves_approved_cold_group_capacity():
    with _session() as session:
        original = session.scalar(select(ManagedPresencePolicyRevision))
        session.delete(original)
        session.flush()
        policy = _ensure_presence_policy(session, 1)
        assert policy.max_consecutive_system_turns == 2
        assert policy.absolute_daily_authored_cap == 20
        assert policy.managed_to_external_ratio_bps == 10000
        assert policy.bootstrap_allowance == 2

        decision = ensure_natural_opportunity_plan(
            session, session.get(Task, "group-presence"),
            session.get(TaskDayLedger, "group-presence-day"),
            group=session.get(TgGroup, 21), required_units=3,
        )
        assert decision.guaranteed_now_capacity == 2
        assert decision.deficit == 1


def test_presence_initialization_preserves_existing_revision():
    with _session() as session:
        original = session.scalar(select(ManagedPresencePolicyRevision))
        original.max_consecutive_system_turns = 1
        original.absolute_daily_authored_cap = 10
        original.bootstrap_allowance = 1
        session.commit()

        policy = _ensure_presence_policy(session, 1)

        assert policy.id == original.id
        assert policy.max_consecutive_system_turns == 1
        assert policy.absolute_daily_authored_cap == 10
        assert policy.bootstrap_allowance == 1
