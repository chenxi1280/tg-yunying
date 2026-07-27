from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, DispatchClaimReservation, DispatchClaimScope, DispatchClaimShardAllocation, DispatchClaimWindow, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatch_claim_selection import build_demands
from app.services.task_center import account_pool
from app.services.task_center.dispatch_reservations import task_dispatch_claim_snapshot
from app.services.task_center.service import get_task_detail
from app.timezone import BEIJING_TZ


pytestmark = pytest.mark.no_postgres


@pytest.fixture(autouse=True)
def clear_runtime_reservations():
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()
    yield
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def test_strict_search_and_hard_hourly_receive_persisted_claim_reservations(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _settings(dispatcher_concurrency=2))
    now_value = _now()

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        claimed = dispatcher.claim_actions(session, limit=2, worker_id="reservation-test")

        assert {action.id for action in claimed} == {"strict-search", "hard-hourly"}
        _assert_claim_metadata(claimed)
        window = session.scalar(select(DispatchClaimWindow))
        allocations = list(session.scalars(select(DispatchClaimShardAllocation)))
        reservations = list(session.scalars(select(DispatchClaimReservation)))
        assert window and window.claim_capacity == 2
        assert window.active_claim_count == 2
        assert window.unclaimed_allocated_count == 0
        assert sum(row.active_claim_count for row in allocations) == 2
        assert {row.claim_class for row in reservations} == {"search_join", "hard_hourly"}
        assert all(row.claimed_count == 1 for row in reservations)
        snapshot = task_dispatch_claim_snapshot(session, session.get(Task, "search-task"))
        assert snapshot["dispatcher_scope"] == "task_center_dispatch"
        assert snapshot["reservations"][0]["claim_class"] == "search_join"
        detail = get_task_detail(session, 1, "search-task")
        assert detail["task"]["stats"]["dispatch_claim"]["reservations"][0]["claim_class"] == "search_join"

        for action in claimed:
            action.status = "success"
            assert dispatcher.release_dispatch_claim(session, action) is True
        session.flush()
        assert window.active_claim_count == 0
        assert sum(row.active_claim_count for row in allocations) == 0


def test_strict_search_join_receives_min_reserved_capacity_against_hard_hourly(monkeypatch) -> None:
    """PRD §2.20.1 RC-4: strict search_join 在 hard_hourly 高需求时仍能拿到 min_reserved_capacity。"""
    from app.services.task_center.dispatch_claim_allocation import _allocate_demands, DispatchClaimDemand

    # 构造 1 个 strict search_join demand + 10 个 strict hard_hourly demand
    search_demand = DispatchClaimDemand(
        tenant_id=1, task_id="search-task", claim_class="search_join",
        shard_total=1, shard_index=0, action_ids=("s1",), required_claims=5, is_strict=True, urgency_score=10,
    )
    hard_demands = [
        DispatchClaimDemand(
            tenant_id=1, task_id=f"hard-task-{i}", claim_class="hard_hourly",
            shard_total=1, shard_index=0, action_ids=(f"h{i}",), required_claims=10, is_strict=True, urgency_score=100,
        )
        for i in range(10)
    ]
    demands = [search_demand, *hard_demands]
    # scope_capacity=10, available=10, min_reserved=max(1, int(10*0.30))=3
    grants = _allocate_demands(demands, available=10, epoch=1, scope_capacity=10)
    search_grant = grants[search_demand.key]
    # search_join 至少拿到 3（30% of 10）
    assert search_grant >= 3, f"search_join should get min_reserved_capacity=3, got {search_grant}"
    # hard_hourly 总和不超过 7
    hard_total = sum(grants[d.key] for d in hard_demands)
    assert hard_total <= 7, f"hard_hourly total should be <= 7, got {hard_total}"
    # 总分配不超过 available
    assert search_grant + hard_total == 10


def test_no_search_join_demands_skips_reserved_allocation() -> None:
    """PRD §2.20.1: 无 strict search_join demand 时不触发预留逻辑。"""
    from app.services.task_center.dispatch_claim_allocation import _allocate_demands, DispatchClaimDemand

    hard_demands = [
        DispatchClaimDemand(
            tenant_id=1, task_id=f"hard-task-{i}", claim_class="hard_hourly",
            shard_total=1, shard_index=0, action_ids=(f"h{i}",), required_claims=5, is_strict=True, urgency_score=100,
        )
        for i in range(3)
    ]
    grants = _allocate_demands(hard_demands, available=10, epoch=1, scope_capacity=10)
    total = sum(grants[d.key] for d in hard_demands)
    assert total == 10, f"all capacity should go to hard_hourly, got total={total}"


def test_dispatch_claim_urgency_normalizes_persisted_timezone() -> None:
    scheduled_at = datetime(2026, 7, 26, 9, 0, tzinfo=BEIJING_TZ)
    task = Task(id="timezone-task", tenant_id=1, name="时区", type="group_relay", status="running")
    action = Action(
        id="timezone-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=scheduled_at,
        created_at=scheduled_at,
        payload={},
    )

    demands = build_demands([action], {task.id: task}, 1, datetime(2026, 7, 26, 9, 5))

    assert len(demands) == 1
    assert demands[0].urgency_score == 400


def test_unserved_strict_demand_is_explicit_when_window_capacity_is_exhausted(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _settings(dispatcher_concurrency=1))
    now_value = _now()

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        claimed = dispatcher.claim_actions(session, limit=1, worker_id="capacity-test")

        assert len(claimed) == 1
        tasks = [session.get(Task, "search-task"), session.get(Task, "hard-task")]
        blocked = [task for task in tasks if task and task.stats.get("dispatch_claim", {}).get("status")]
        assert blocked
        assert all(task.stats["dispatch_claim"]["status"] == "shared_dispatch_capacity_insufficient" for task in blocked)
        assert claimed[0].result["dispatch_unserved_strict_classes"]


def test_two_shards_consume_one_shared_window_without_overallocation(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    settings.account_shard_total = 2
    settings.account_shard_index = 0
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(account_pool, "get_settings", lambda: settings)
    now_value = _now()

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        first = dispatcher.claim_actions(session, limit=1, worker_id="shard-zero")
        settings.account_shard_index = 1
        second = dispatcher.claim_actions(session, limit=1, worker_id="shard-one")

        assert {action.id for action in first + second} == {"strict-search", "hard-hourly"}
        window = session.scalar(select(DispatchClaimWindow))
        allocations = list(session.scalars(select(DispatchClaimShardAllocation)))
        assert window and window.active_claim_count == 2
        assert window.active_claim_count + window.unclaimed_allocated_count <= window.claim_capacity
        assert {(row.account_shard_total, row.account_shard_index) for row in allocations} == {(2, 0), (2, 1)}


def test_cross_window_claims_keep_executing_scope_capacity_reserved(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=1)
    now_value = _now().replace(second=0, microsecond=0)
    clock = {"now": now_value}
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_now", lambda: clock["now"])

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        first = dispatcher.claim_actions(session, limit=1, worker_id="first-window")
        assert len(first) == 1
        assert first[0].status == "executing"

        clock["now"] = now_value + timedelta(seconds=61)
        assert dispatcher.claim_actions(session, limit=1, worker_id="second-window") == []
        scope = session.scalar(select(DispatchClaimScope))
        assert scope is not None
        assert scope.active_claim_count == 1

        first[0].status = "success"
        assert dispatcher.release_dispatch_claim(session, first[0]) is True
        assert len(dispatcher.claim_actions(session, limit=1, worker_id="released-window")) == 1


def test_release_reconciles_drifted_old_window_counters(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: _settings(dispatcher_concurrency=1))
    now_value = _now().replace(second=0, microsecond=0)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        action = dispatcher.claim_actions(session, limit=1, worker_id="old-window")[0]
        scope = session.scalar(select(DispatchClaimScope))
        window = session.scalar(select(DispatchClaimWindow))
        allocation = session.scalar(select(DispatchClaimShardAllocation))
        assert scope is not None and window is not None and allocation is not None
        action.status = "pending"
        window.active_claim_count = 0
        allocation.active_claim_count = 0
        session.flush()

        assert dispatcher.release_dispatch_claim(session, action) is True

        assert scope.active_claim_count == 0
        assert window.active_claim_count == 0
        assert allocation.active_claim_count == 0
        assert action.result["dispatch_claim_active"] is False
        reconciliation = action.result["dispatch_claim_release_reconciliation"]
        assert reconciliation["drifted"] is True
        assert reconciliation["window"]["before"] == 0
        assert reconciliation["window"]["after"] == 0


def test_terminal_claim_without_finalizer_is_reconciled_before_window_reallocation(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=1)
    now_value = _now().replace(second=0, microsecond=0)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_now", lambda: now_value)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        first = dispatcher.claim_actions(session, limit=1, worker_id="stale-terminal")
        assert len(first) == 1
        first[0].status = "skipped"
        session.commit()

        second = dispatcher.claim_actions(session, limit=1, worker_id="reallocated")

        assert len(second) == 1
        assert second[0].id != first[0].id
        window = session.scalar(select(DispatchClaimWindow))
        allocation = session.scalar(select(DispatchClaimShardAllocation))
        assert window is not None and window.active_claim_count == 1
        assert allocation is not None and allocation.active_claim_count == 1


def test_stale_unclaimed_reservation_is_released_for_new_due_action(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now().replace(second=0, microsecond=0)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_now", lambda: now_value)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        first = dispatcher.claim_actions(session, limit=1, worker_id="stale-unclaimed")

        assert len(first) == 1
        window = session.scalar(select(DispatchClaimWindow))
        assert window is not None and window.unclaimed_allocated_count == 1
        for action_id in ("strict-search", "hard-hourly"):
            session.get(Action, action_id).status = "skipped"
        session.add(
            Task(id="fresh-task", tenant_id=1, name="新任务", type="group_relay", status="running")
        )
        session.add(
            Action(
                id="fresh-action",
                tenant_id=1,
                task_id="fresh-task",
                task_type="group_relay",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"message_text": "新任务"},
            )
        )
        session.commit()

        second = dispatcher.claim_actions(session, limit=1, worker_id="fresh-claim")

        assert [action.id for action in second] == ["fresh-action"]
        stale = list(session.scalars(select(DispatchClaimReservation).where(DispatchClaimReservation.task_id != "fresh-task")))
        assert any(row.reason == "unclaimed_action_no_longer_due" for row in stale)


def test_locked_claim_plan_candidates_preserve_planned_order_over_old_backlog() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(id="plan-task", tenant_id=1, name="计划顺序", type="group_relay", status="running"))
        session.add_all(
            [
                Action(
                    id="planned-first",
                    tenant_id=1,
                    task_id="plan-task",
                    task_type="group_relay",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=now_value - timedelta(seconds=1),
                    payload={"message_text": "计划优先"},
                ),
                Action(
                    id="old-backlog",
                    tenant_id=1,
                    task_id="plan-task",
                    task_type="group_relay",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=now_value - timedelta(days=1),
                    payload={"message_text": "历史积压"},
                ),
            ]
        )
        session.commit()

        rows = dispatcher._locked_claim_plan_candidates(
            session,
            SimpleNamespace(candidate_action_ids=("planned-first", "old-backlog")),
            1,
            now_value,
        )

        assert [row.id for row in rows] == ["planned-first"]


def _seed_strict_actions(session: Session, now_value) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add_all(
        [
            TgAccount(id=11, tenant_id=1, display_name="搜索账号", phone_masked="+861***0011", status="在线"),
            TgAccount(id=12, tenant_id=1, display_name="硬小时账号", phone_masked="+861***0012", status="在线"),
            Task(
                id="search-task",
                tenant_id=1,
                name="严格搜索",
                type="search_join_group",
                status="running",
                type_config={"strict_daily_target": True, "daily_click_target_count": 20},
            ),
            Task(id="hard-task", tenant_id=1, name="硬小时", type="group_ai_chat", status="running"),
            Action(
                id="strict-search",
                tenant_id=1,
                task_id="search-task",
                task_type="search_join_group",
                action_type="search_join",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={},
            ),
            Action(
                id="hard-hourly",
                tenant_id=1,
                task_id="hard-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=12,
                status="pending",
                scheduled_at=now_value - timedelta(seconds=1),
                payload={"message_text": "硬小时动作", "hard_hourly_target": True},
            ),
        ]
    )
    session.commit()


def _assert_claim_metadata(actions: list[Action]) -> None:
    keys = {
        "dispatch_claim_class",
        "dispatch_reservation_id",
        "dispatch_claim_window_id",
        "dispatch_claim_shard_allocation_id",
        "dispatch_claim_scope",
        "dispatch_claim_shard",
        "dispatch_allocation_epoch",
        "dispatch_reservation_reason",
        "dispatch_urgency_score",
        "dispatch_unserved_strict_classes",
    }
    for action in actions:
        assert keys <= set(action.result)


def _settings(*, dispatcher_concurrency: int):
    return SimpleNamespace(
        enable_redis_token_bucket=False,
        action_claim_limit=10,
        action_claim_seconds=60,
        action_lease_seconds=1800,
        dispatcher_concurrency=dispatcher_concurrency,
        account_shard_total=1,
        account_shard_index=0,
        enable_redis_account_inflight=False,
        redis_account_inflight_seconds=1800,
    )
