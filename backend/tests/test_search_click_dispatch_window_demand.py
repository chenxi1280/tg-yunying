from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    OperationTarget,
    SearchClickFulfillmentObligation,
    Task,
    TaskDayLedger,
    Tenant,
)
from app.services._common import _now
from app.services.task_center.dispatch_claim_ledger import (
    current_window_allocations,
    window_reservations,
)
from app.services.task_center.dispatch_claim_reconciliation import (
    reconcile_window_unclaimed,
)
from app.services.task_center.dispatch_claim_selection import build_demands
from app.services.task_center.dispatch_reservations import (
    _prepare_dispatch_window,
)
from app.services.task_center.search_click_dispatch_allocation import _all_demands
from app.services.task_center.search_click_assignment_release import (
    release_search_click_assignment,
)
from search_click_assignment_test_support import seed_assignment


pytestmark = pytest.mark.no_postgres
SEARCH_RESERVATION_KEY = (1, "search-task", "search_join", 1, 0)


def test_ordinary_dispatcher_allocates_and_retains_open_search_obligation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        action = _seed_rows(session, now_value)
        demands = build_demands(
            [action],
            {"ordinary-task": session.get(Task, "ordinary-task")},
            1,
            now_value,
        )
        _, window, _ = _prepare_dispatch_window(
            session,
            [action],
            demands,
            settings=_settings(),
            now=now_value,
        )
        reservations = window_reservations(session, window.id)

        assert reservations[SEARCH_RESERVATION_KEY].reserved_claims == 1
        session.get(Task, "search-task").status = "stopped"
        session.flush()
        released = reconcile_window_unclaimed(
            session,
            window,
            allocations=current_window_allocations(session, window),
            reservations=reservations,
            now=now_value,
        )
        assert released == 0
        assert reservations[SEARCH_RESERVATION_KEY].reserved_claims == 1


def test_search_planner_preserves_runtime_shards_for_ordinary_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        _seed_rows(session, now_value)

        demands = _all_demands(
            session,
            now_value,
            shard_total=4,
        )
        by_task = {demand.task_id: demand for demand in demands}

        assert (
            by_task["ordinary-task"].shard_total,
            by_task["ordinary-task"].shard_index,
        ) == (4, 3)
        assert (
            by_task["search-task"].shard_total,
            by_task["search-task"].shard_index,
        ) == (1, 0)


def test_reconciliation_preserves_bound_search_unit_until_exact_release() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        seed_assignment(session)
        window = session.get(DispatchClaimWindow, "window-1")
        allocation = session.get(DispatchClaimShardAllocation, "shard-1")
        reservation = session.get(DispatchClaimReservation, "reservation-1")
        reservations = {
            (1, "task-1", "search_join", 1, 0): reservation,
        }

        reconcile_window_unclaimed(
            session,
            window,
            allocations=[allocation],
            reservations=reservations,
            now=_now(),
        )

        assert allocation.unclaimed_allocated_count == 1
        assert window.unclaimed_allocated_count == 1
        release_search_click_assignment(
            session,
            "assignment-1",
            trigger_key="account-policy:action-1:0",
            reason_code="search_resource_saturated",
            now_value=_now(),
        )
        assert allocation.unclaimed_allocated_count == 0
        assert window.unclaimed_allocated_count == 0


def test_release_quarantine_repairs_stale_bound_unclaimed_counters() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        seed_assignment(session)
        window = session.get(DispatchClaimWindow, "window-1")
        allocation = session.get(DispatchClaimShardAllocation, "shard-1")
        window.unclaimed_allocated_count = 0
        allocation.unclaimed_allocated_count = 0
        session.commit()

        batch = release_search_click_assignment(
            session,
            "assignment-1",
            trigger_key="repair-stale-unclaimed:action-1:0",
            reason_code="search_resource_saturated",
            now_value=_now(),
        )

        reservation = session.get(DispatchClaimReservation, "reservation-1")
        assert batch.release_unit_count == 1
        assert reservation.bound_count == 0
        assert reservation.released_count == 1
        assert allocation.unclaimed_allocated_count == 0
        assert window.unclaimed_allocated_count == 0


def _seed_rows(session: Session, now_value: datetime) -> Action:
    session.add(Tenant(id=1, name="t"))
    _add_target_and_tasks(session)
    _add_search_obligation(session, now_value)
    action = _add_ordinary_action(session, now_value)
    session.commit()
    return action


def _add_target_and_tasks(session: Session) -> None:
    session.add(OperationTarget(
        id=1,
        tenant_id=1,
        target_type="group",
        tg_peer_id="target",
        username="target",
        title="target",
    ))
    session.add_all([
        Task(
            id="search-task",
            tenant_id=1,
            name="纯搜索",
            type="search_click",
            status="running",
        ),
        Task(
            id="ordinary-task",
            tenant_id=1,
            name="普通任务",
            type="channel_comment",
            status="running",
        ),
    ])


def _add_search_obligation(session: Session, now_value: datetime) -> None:
    ledger = TaskDayLedger(
        id="search-ledger",
        tenant_id=1,
        task_id="search-task",
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=now_value.date(),
        period_start_at=now_value,
        deadline_at=now_value + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=now_value,
    )
    session.add(ledger)
    session.add(SearchClickFulfillmentObligation(
        id="search-obligation",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=1,
        click_obligation_ordinal=1,
    ))


def _add_ordinary_action(
    session: Session,
    now_value: datetime,
) -> Action:
    action = Action(
        id="ordinary-action",
        tenant_id=1,
        task_id="ordinary-task",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=11,
        status="pending",
        scheduled_at=now_value,
        payload={},
    )
    session.add(action)
    return action


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        action_claim_limit=10,
        dispatcher_concurrency=2,
    )
