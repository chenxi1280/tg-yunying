from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorSessionPlan,
    AccountPacingReservation,
    Action,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.account_pacing_guard import (
    bind_account_pacing_reservation,
    reserve_account_pacing,
    revalidate_action_pacing_before_claim,
)
from app.services.task_center.account_pacing_guard import (
    ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION,
    ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION,
)
from app.services.task_center import account_pacing_guard
from app.services.task_center.engagement_behavior_sessions import (
    behavior_session_not_before,
    ensure_behavior_session_plan,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session) -> tuple[Task, Task]:
    session.add(Tenant(id=1, name="测试租户"))
    session.add(
        TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号11",
            phone_masked="11",
            status="在线",
        )
    )
    session.add(
        AccountBehaviorBudgetPolicyRevision(
            id="behavior-policy-1",
            tenant_id=1,
            account_class="normal",
            action_budgets={"authored": 8, "reaction": 20, "view": 40},
            session_budget={},
            pair_gap_policy={
                "authored_to_authored_seconds": 300,
                "passive_to_authored_seconds": 300,
            },
            wake_budget=2,
        )
    )
    tasks = (
        Task(
            id="group-task",
            tenant_id=1,
            name="活群",
            type="group_ai_chat",
            type_config={"engagement_contract_version": "unified_engagement_v1"},
        ),
        Task(
            id="view-task",
            tenant_id=1,
            name="浏览",
            type="channel_view",
            type_config={"engagement_contract_version": "unified_engagement_v1"},
        ),
    )
    session.add_all(tasks)
    session.commit()
    return tasks


def _window_times(plan: AccountBehaviorSessionPlan) -> list[tuple[datetime, datetime]]:
    return [
        (datetime.fromisoformat(item["start_at"]), datetime.fromisoformat(item["end_at"]))
        for item in plan.windows
    ]


def test_daily_plan_is_stable_bounded_and_shared_across_tasks() -> None:
    with _session() as session:
        _seed(session)
        task_day = datetime(2026, 9, 4).date()
        first = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=task_day,
        )
        second = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=task_day,
        )
        windows = _window_times(first)

        assert first.id == second.id
        assert 2 <= len(windows) <= 4
        assert all(15 <= (end - start).total_seconds() / 60 <= 45 for start, end in windows)
        assert all(previous[1] <= current[0] for previous, current in zip(windows, windows[1:]))
        assert session.scalar(select(func.count(AccountBehaviorSessionPlan.id))) == 1


def test_window_gate_converts_timezone_and_moves_to_next_session() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        first_start, _ = _window_times(plan)[0]

        assert (
            behavior_session_not_before(
                session,
                task_id=group_task.id,
                account_id=11,
                desired_at=datetime(2026, 9, 4),
                deadline_at=first_start,
            )
            is None
        )
        first_start, first_end = _window_times(plan)[0]
        utc_before = (first_start - timedelta(minutes=5)).replace(
            tzinfo=timezone(timedelta(hours=8))
        ).astimezone(timezone.utc)

        next_at = behavior_session_not_before(
            session,
            task_id=group_task.id,
            account_id=11,
            desired_at=utc_before,
            deadline_at=first_end,
        )

        assert next_at == first_start


def test_reservation_uses_session_floor_and_honors_source_deadline() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        first_start, _ = _window_times(plan)[0]
        due_at = datetime(2026, 9, 4)
        reservation = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:1",
            due_at=due_at,
            deadline_at=first_start + timedelta(minutes=1),
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )

        assert reservation.effective_claim_at == first_start
        assert session.scalar(select(func.count(AccountPacingReservation.id))) == 1


def test_no_window_before_deadline_is_explicitly_rejected() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        first_start, _ = _window_times(plan)[0]
        due_at = first_start - timedelta(minutes=1)

        with pytest.raises(
            account_pacing_guard.AccountPacingDeadlineExceeded,
            match="account_behavior_session_unavailable",
        ):
            reserve_account_pacing(
                session,
                tenant_id=1,
                task_id=group_task.id,
                account_id=11,
                slot_key="group:no-window",
                due_at=due_at,
                deadline_at=due_at + timedelta(seconds=30),
                engagement_contract_version="unified_engagement_v1",
                action_class="authored_message",
            )


def test_human_turn_can_use_bounded_session_wake() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        due_at = datetime(2026, 9, 4)

        reservation = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:human-turn",
            due_at=due_at,
            deadline_at=due_at + timedelta(seconds=30),
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
            allow_session_wake=True,
        )

        assert reservation.effective_claim_at == due_at
        assert reservation.policy_version == (
            ACCOUNT_BEHAVIOR_SESSION_WAKE_POLICY_VERSION
        )
        assert session.scalar(select(AccountBehaviorBudgetLedger)) is None
        action = Action(
            tenant_id=1,
            task_id=group_task.id,
            task_type=group_task.type,
            action_type="send_message",
            account_id=11,
            scheduled_at=due_at,
            pacing_due_at=due_at,
            release_not_before_at=due_at,
            pacing_slot_key=reservation.pacing_slot_key,
        )
        session.add(action)
        session.flush()
        bind_account_pacing_reservation(reservation, action)

        decision = revalidate_action_pacing_before_claim(
            session, action, now_value=due_at,
        )

        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert decision.allowed
        assert reservation.policy_version == (
            ACCOUNT_BEHAVIOR_SESSION_WAKE_CONSUMED_POLICY_VERSION
        )
        assert ledger is not None and ledger.wake_count == 1


def test_human_turn_wake_budget_is_not_silently_exceeded() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        day_start = datetime(2026, 9, 4)
        for index in range(2):
            due_at = day_start + timedelta(minutes=5 * index)
            reserve_account_pacing(
                session,
                tenant_id=1,
                task_id=group_task.id,
                account_id=11,
                slot_key=f"group:wake:{index}",
                due_at=due_at,
                deadline_at=due_at + timedelta(seconds=30),
                engagement_contract_version="unified_engagement_v1",
                action_class="authored_message",
                allow_session_wake=True,
            )

        blocked_at = day_start + timedelta(minutes=10)
        with pytest.raises(
            account_pacing_guard.AccountPacingDeadlineExceeded,
            match="account_behavior_session_unavailable",
        ):
            reserve_account_pacing(
                session,
                tenant_id=1,
                task_id=group_task.id,
                account_id=11,
                slot_key="group:wake:blocked",
                due_at=blocked_at,
                deadline_at=blocked_at + timedelta(seconds=30),
                engagement_contract_version="unified_engagement_v1",
                action_class="authored_message",
                allow_session_wake=True,
            )
        assert session.scalar(select(AccountBehaviorBudgetLedger)) is None
        assert session.scalar(select(func.count(AccountPacingReservation.id))) == 2


def test_after_last_window_rolls_to_next_day_when_deadline_allows() -> None:
    with _session() as session:
        group_task, _ = _seed(session)
        current = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        _, last_end = _window_times(current)[-1]
        next_at = behavior_session_not_before(
            session,
            task_id=group_task.id,
            account_id=11,
            desired_at=last_end + timedelta(minutes=1),
            deadline_at=datetime(2026, 9, 5, 23, 59),
        )
        next_plan = session.scalar(
            select(AccountBehaviorSessionPlan).where(
                AccountBehaviorSessionPlan.task_day == datetime(2026, 9, 5).date()
            )
        )

        assert next_plan is not None
        assert next_at == _window_times(next_plan)[0][0]


def test_passive_then_authored_uses_directional_cross_task_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: type("Settings", (), {"account_soft_pacing_min_gap_seconds": 20})(),
    )
    with _session() as session:
        group_task, view_task = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        start_at, end_at = _window_times(plan)[0]
        view = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=view_task.id,
            account_id=11,
            slot_key="view:1",
            due_at=start_at,
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="view",
        )
        authored = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:2",
            due_at=start_at,
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )

        assert view.effective_claim_at == start_at
        assert authored.effective_claim_at == start_at + timedelta(minutes=5)


def test_rearmed_unified_slot_keeps_directional_cross_task_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: type("Settings", (), {"account_soft_pacing_min_gap_seconds": 20})(),
    )
    with _session() as session:
        group_task, view_task = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        start_at, end_at = _window_times(plan)[0]
        authored = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:rearmed",
            due_at=start_at,
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )
        passive = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=view_task.id,
            account_id=11,
            slot_key="view:before-rearm",
            due_at=start_at + timedelta(minutes=10),
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="view",
        )

        rearmed = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:rearmed",
            due_at=passive.effective_claim_at,
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )

        assert rearmed.id == authored.id
        assert rearmed.effective_claim_at == (
            passive.effective_claim_at + timedelta(minutes=5)
        )


def test_future_authored_slot_moves_new_passive_action_after_it(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: type("Settings", (), {"account_soft_pacing_min_gap_seconds": 20})(),
    )
    with _session() as session:
        group_task, view_task = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        start_at, end_at = _window_times(plan)[0]
        authored = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:future",
            due_at=start_at + timedelta(seconds=100),
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )
        passive = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=view_task.id,
            account_id=11,
            slot_key="view:new",
            due_at=start_at,
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="view",
        )

        assert authored.effective_claim_at == start_at + timedelta(seconds=100)
        assert passive.effective_claim_at == authored.effective_claim_at + timedelta(seconds=20)


def test_pair_conflict_just_after_deadline_cannot_be_ignored(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: type("Settings", (), {"account_soft_pacing_min_gap_seconds": 20})(),
    )
    with _session() as session:
        group_task, view_task = _seed(session)
        plan = ensure_behavior_session_plan(
            session,
            tenant_id=1,
            account_id=11,
            task_day=datetime(2026, 9, 4).date(),
        )
        start_at, end_at = _window_times(plan)[0]
        deadline = start_at + timedelta(minutes=2)
        reserve_account_pacing(
            session,
            tenant_id=1,
            task_id=group_task.id,
            account_id=11,
            slot_key="group:after-deadline",
            due_at=deadline + timedelta(minutes=1),
            deadline_at=end_at,
            engagement_contract_version="unified_engagement_v1",
            action_class="authored_message",
        )

        with pytest.raises(
            account_pacing_guard.AccountPacingDeadlineExceeded,
            match="account_timeline_conflict",
        ):
            reserve_account_pacing(
                session,
                tenant_id=1,
                task_id=view_task.id,
                account_id=11,
                slot_key="view:before-deadline",
                due_at=start_at,
                deadline_at=deadline,
                engagement_contract_version="unified_engagement_v1",
                action_class="view",
            )
