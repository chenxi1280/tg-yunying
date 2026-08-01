from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, DispatchClaimReservation, DispatchClaimScope, DispatchClaimShardAllocation, DispatchClaimWindow, GroupBotAdmission, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.dispatch_claim_allocation import allocate_window
from app.services.task_center.dispatch_claim_selection import build_demands
from app.services.task_center.dispatch_claim_types import DispatchActionCandidate
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


def test_rebuild_preserves_unclaimed_reservations_from_prior_epoch() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now().replace(second=0, microsecond=0)

    with Session(engine) as session:
        scope = DispatchClaimScope(
            dispatcher_scope="task_center_dispatch",
            claim_capacity=52,
        )
        window = DispatchClaimWindow(
            dispatcher_scope="task_center_dispatch",
            bucket_start=now_value,
            bucket_end=now_value + timedelta(minutes=1),
            claim_capacity=52,
            allocation_epoch=1,
            allocation_state="rebuild_required",
            unclaimed_allocated_count=5,
        )
        session.add_all([scope, window])
        session.flush()
        allocation = DispatchClaimShardAllocation(
            dispatch_claim_window_id=window.id,
            dispatch_allocation_epoch=1,
            account_shard_total=1,
            account_shard_index=0,
            unclaimed_allocated_count=5,
        )
        session.add(allocation)
        session.flush()

        allocate_window(session, scope, window, [allocation], [])

        assert window.allocation_epoch == 2
        assert window.unclaimed_allocated_count == 5
        assert allocation.unclaimed_allocated_count == 5


def test_no_task_type_has_a_fixed_global_reserved_share() -> None:
    from app.services.task_center.dispatch_claim_allocation import _allocate_demands, DispatchClaimDemand

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
    served_tasks: set[str] = set()

    for epoch in range(1, 61):
        grants = _allocate_demands(demands, available=10, epoch=epoch)
        assert sum(grants.values()) == 10
        assert all(grant <= 1 for grant in grants.values())
        served_tasks.update(
            demand.task_id for demand in demands if grants[demand.key] > 0
        )

    assert served_tasks == {demand.task_id for demand in demands}


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


def test_target_admission_backlog_preserves_one_claim_per_ordinary_demand() -> None:
    from app.services.task_center.dispatch_claim_allocation import _allocate_demands, DispatchClaimDemand

    admission = DispatchClaimDemand(
        tenant_id=1, task_id="admission-task", claim_class="target_admission_retry",
        shard_total=1, shard_index=0, action_ids=("a1",), required_claims=100, is_strict=True, urgency_score=1_000_000,
    )
    ordinary = [
        DispatchClaimDemand(
            tenant_id=1, task_id=f"ai-task-{index}", claim_class="ordinary",
            shard_total=1, shard_index=0, action_ids=(f"o{index}",), required_claims=10,
            is_strict=False, urgency_score=100,
        )
        for index in range(3)
    ]

    grants = _allocate_demands([admission, *ordinary], available=20, epoch=1, scope_capacity=20)

    assert grants[admission.key] < 17
    assert all(grants[demand.key] >= 1 for demand in ordinary)
    assert sum(grants.values()) == 20


def test_huge_search_debt_cannot_starve_mixed_fulfillment_parents() -> None:
    from app.services.task_center.dispatch_claim_allocation import (
        DispatchClaimDemand,
        _allocate_demands,
    )

    search = [
        DispatchClaimDemand(
            tenant_id=1,
            task_id=f"search-shard-{shard}",
            allocation_business_task_id="search-task",
            claim_class="search_join",
            shard_total=2,
            shard_index=shard,
            action_ids=(f"search-{shard}",),
            required_claims=5_000,
            is_strict=True,
            urgency_score=1_000_000,
        )
        for shard in range(2)
    ]
    ordinary = [
        DispatchClaimDemand(
            tenant_id=1,
            task_id=task_id,
            claim_class="ordinary",
            shard_total=2,
            shard_index=index % 2,
            action_ids=(f"action-{index}",),
            required_claims=4,
            is_strict=False,
            urgency_score=100 - index,
        )
        for index, task_id in enumerate(
            ("ai-1", "ai-2", "ai-3", "comment", "reaction", "view"),
        )
    ]
    demands = [*search, *ordinary]

    for epoch in range(1, 9):
        grants = _allocate_demands(demands, available=26, epoch=epoch)
        search_grants = sum(grants[demand.key] for demand in search)
        assert sum(grants.values()) == 26
        assert search_grants < 26
        assert all(grants[demand.key] >= 1 for demand in ordinary)


def test_priority_admission_and_search_membership_share_capacity() -> None:
    from app.services.task_center.dispatch_claim_allocation import _allocate_demands, DispatchClaimDemand

    demands = [
        DispatchClaimDemand(
            tenant_id=1, task_id="admission-task", claim_class="target_admission_retry",
            shard_total=1, shard_index=0, action_ids=("a1",), required_claims=10,
            is_strict=True, urgency_score=1_000_000,
        ),
        DispatchClaimDemand(
            tenant_id=1, task_id="search-task", claim_class="search_join_membership",
            shard_total=1, shard_index=0, action_ids=("m1",), required_claims=10,
            is_strict=True, urgency_score=500_000,
        ),
    ]

    grants = _allocate_demands(demands, available=4, epoch=1, scope_capacity=4)

    assert all(grants[demand.key] >= 1 for demand in demands)
    assert sum(grants.values()) == 4


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
        assert all(
            not (task and task.stats.get("dispatch_claim"))
            for task in tasks
        )
        assert claimed[0].result["dispatch_unserved_strict_classes"]


def test_ready_window_keeps_epoch_while_reservations_remain() -> None:
    from app.services.task_center.dispatch_reservations import (
        _input_change_requires_rebuild,
    )

    window = SimpleNamespace(
        allocation_state="ready",
        unclaimed_allocated_count=1,
        effective_unclaimed_count=1,
        ready_rebuild_snapshot_hash="old-hash",
    )

    assert _input_change_requires_rebuild(
        window,
        demand_hash="new-hash",
        demand_without_reservation=True,
    ) is False


def test_dispatch_claim_scope_locks_before_global_candidate_scan(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    events: list[str] = []
    original_scan = dispatcher._dispatch_claim_window_actions

    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(
        dispatcher,
        "lock_dispatch_claim_selection",
        lambda *_args, **_kwargs: events.append("scope_lock"),
        raising=False,
    )

    def record_scan(*args, **kwargs):
        events.append("candidate_scan")
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(
        dispatcher,
        "_dispatch_claim_window_actions",
        record_scan,
    )

    with Session(engine) as session:
        _seed_strict_actions(session, _now())
        dispatcher.claim_actions(session, limit=1, worker_id="lock-order")

    assert events[:2] == ["scope_lock", "candidate_scan"]


def test_dispatch_claim_window_scan_defers_full_result_payload(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        rows = dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= now_value],
            settings=settings,
            now_value=now_value,
            force_ordinary_tenants=set(),
        )

        assert rows
        assert all(isinstance(action, DispatchActionCandidate) for action in rows)


def test_locked_plan_candidate_reloads_deferred_result(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        rows = dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= now_value],
            settings=settings,
            now_value=now_value,
            force_ordinary_tenants=set(),
        )
        locked = dispatcher._locked_claim_plan_candidates(
            session,
            SimpleNamespace(candidate_action_ids=(rows[0].id,)),
            1,
            now_value,
            set(),
        )

        assert locked
        assert isinstance(rows[0], DispatchActionCandidate)
        assert "result" not in inspect(locked[0]).unloaded


def test_ordinary_candidate_scan_keeps_each_due_task_visible(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add_all([
            Task(id="blocked-task", tenant_id=1, name="阻塞任务", type="group_ai_chat", status="running", priority=1),
            Task(id="ready-task", tenant_id=1, name="可执行任务", type="group_ai_chat", status="running", priority=9),
        ])
        session.add_all([
            Action(
                id=f"blocked-{index}",
                tenant_id=1,
                task_id="blocked-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=10 + index,
                status="pending",
                scheduled_at=now_value - timedelta(minutes=2),
                payload={},
            )
            for index in range(3)
        ])
        session.add(Action(
            id="ready",
            tenant_id=1,
            task_id="ready-task",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=20,
            status="pending",
            scheduled_at=now_value - timedelta(minutes=1),
            payload={},
        ))
        session.flush()

        rows = dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= now_value],
            settings=settings,
            now_value=now_value,
            force_ordinary_tenants=set(),
        )

        assert "ready" in {action.id for action in rows}


def test_bounded_candidate_scan_does_not_rank_entire_due_action_set(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    statements: list[str] = []
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )

    with Session(engine) as session:
        _seed_strict_actions(session, _now())
        dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= _now()],
            settings=settings,
            now_value=_now(),
            force_ordinary_tenants=set(),
        )

    candidate_sql = " ".join(statement.lower() for statement in statements)
    assert "actions" in candidate_sql
    assert "row_number" not in candidate_sql


def test_dispatcher_claim_filters_out_unprepared_ai_generation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="tenant"),
            Task(
                id="ai-generation-task",
                tenant_id=1,
                name="AI生成前置",
                type="group_ai_chat",
                status="running",
            ),
        ])
        session.add_all([
            Action(
                id="generation-pending",
                tenant_id=1,
                task_id="ai-generation-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                    payload={
                        "message_text": "",
                        "ai_generation_status": "pending",
                        "content_scope_contract_version": "group_content_scope_v1",
                    },
            ),
            Action(
                id="generation-ready",
                tenant_id=1,
                task_id="ai-generation-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=12,
                status="pending",
                scheduled_at=now_value,
                    payload={
                        "message_text": "已生成",
                        "ai_generation_status": "ready",
                        "content_scope_contract_version": "group_content_scope_v1",
                    },
            ),
        ])
        session.flush()

        rows = list(session.scalars(
            select(Action)
            .join(Task, Task.id == Action.task_id)
            .where(*dispatcher._claim_base_filters(now_value, None))
        ))

    assert [row.id for row in rows] == ["generation-ready"]


def test_group_ai_membership_backlog_keeps_send_candidate_visible(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    with Session(engine) as session:
        task = Task(
            id="ai-membership-backlog",
            tenant_id=1,
            name="准入与发送并行",
            type="group_ai_chat",
            status="running",
        )
        session.add_all([Tenant(id=1, name="t"), task])
        session.add_all([
            Action(
                id=f"membership-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="ensure_target_membership",
                account_id=100 + index,
                status="pending",
                scheduled_at=now_value - timedelta(minutes=2),
                payload={},
            )
            for index in range(5)
        ])
        session.add(Action(
            id="send-ready",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=200,
            status="pending",
            scheduled_at=now_value - timedelta(minutes=1),
            payload={},
        ))
        session.flush()

        rows = dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= now_value],
            settings=settings,
            now_value=now_value,
            force_ordinary_tenants=set(),
        )
        demands = build_demands(rows, {task.id: task}, 1, now_value)

        assert "send-ready" in {action.id for action in rows}
        assert {demand.claim_class for demand in demands} == {
            "target_admission_retry",
            "ordinary",
        }


@pytest.mark.parametrize(
    "claimable_state",
    ("group_bot_admission_ready", "post_follow_visibility_probe"),
)
def test_group_ai_ready_admission_precedes_older_waiting_send(
    monkeypatch,
    claimable_state: str,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=1)
    now_value = _now()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    with Session(engine) as session:
        task = Task(
            id="ai-admission-priority",
            tenant_id=1,
            name="准入可发优先",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "group_bot_admission_required": True,
            },
        )
        session.add_all([Tenant(id=1, name="t"), task])
        session.add_all([
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=11,
                state="group_bot_policy_unresolved",
            ),
            GroupBotAdmission(
                tenant_id=1,
                group_id=7,
                account_id=12,
                state=claimable_state,
            ),
            Action(
                id="waiting-send",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value - timedelta(hours=1),
                payload={"group_id": 7},
            ),
            Action(
                id="ready-send",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=12,
                status="pending",
                scheduled_at=now_value - timedelta(minutes=1),
                payload={"group_id": 7},
            ),
        ])
        session.flush()

        rows = dispatcher._dispatch_claim_window_actions(
            session,
            [Action.status == "pending", Action.scheduled_at <= now_value],
            settings=settings,
            now_value=now_value,
            force_ordinary_tenants=set(),
        )

        assert [action.id for action in rows] == ["ready-send"]


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
        allocation = session.scalar(
            select(DispatchClaimShardAllocation).where(
                DispatchClaimShardAllocation.dispatch_allocation_epoch
                == window.allocation_epoch
            )
        )
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
        allocation = session.scalar(
            select(DispatchClaimShardAllocation).where(
                DispatchClaimShardAllocation.dispatch_allocation_epoch
                == window.allocation_epoch
            )
        )
        assert window is not None and window.active_claim_count == 1
        assert allocation is not None and allocation.active_claim_count == 1


def test_retry_keeps_epoch_when_another_reservation_is_available(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    settings = _settings(dispatcher_concurrency=2)
    now_value = _now().replace(second=0, microsecond=0)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "_now", lambda: now_value)

    with Session(engine) as session:
        _seed_strict_actions(session, now_value)
        first = dispatcher.claim_actions(session, limit=1, worker_id="first-attempt")[0]
        first.status = "pending"
        dispatcher._release_runtime_resources(first)
        assert dispatcher.release_dispatch_claim(session, first) is True
        initial_epoch = session.scalar(select(DispatchClaimWindow)).allocation_epoch

        retry = dispatcher.claim_actions(session, limit=1, worker_id="retry-attempt")

        assert len(retry) == 1
        assert session.scalar(select(DispatchClaimWindow)).allocation_epoch == initial_epoch


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
            set(),
        )

        assert [row.id for row in rows] == ["planned-first"]


def test_locked_claim_plan_candidates_keep_claim_priority_before_plan_order() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all(
            [
                Task(id="hard-task", tenant_id=1, name="硬小时", type="group_ai_chat", status="running", priority=3),
                Task(
                    id="search-task",
                    tenant_id=1,
                    name="严格搜索",
                    type="search_join_group",
                    status="running",
                    priority=3,
                    type_config={"strict_daily_target": True},
                ),
                Action(
                    id="source-first-in-plan",
                    tenant_id=1,
                    task_id="search-task",
                    task_type="search_join_group",
                    action_type="search_join",
                    status="pending",
                    scheduled_at=now_value - timedelta(days=1),
                    payload={},
                ),
                Action(
                    id="hard-second-in-plan",
                    tenant_id=1,
                    task_id="hard-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=now_value - timedelta(seconds=1),
                    payload={"hard_hourly_target": True},
                ),
            ]
        )
        session.commit()

        rows = dispatcher._locked_claim_plan_candidates(
            session,
            SimpleNamespace(candidate_action_ids=("source-first-in-plan", "hard-second-in-plan")),
            1,
            now_value,
            set(),
        )

        assert [row.id for row in rows] == ["hard-second-in-plan"]


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
                payload={
                    "message_text": "硬小时动作",
                    "hard_hourly_target": True,
                    "content_scope_contract_version": "group_content_scope_v1",
                },
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
