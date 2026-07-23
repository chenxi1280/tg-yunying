from __future__ import annotations

import sys
from datetime import datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AccountPool,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.security import encrypt_session
from app.services.task_center.executors.group_ai_chat import (
    _coverage_capacity_blocker,
    _coverage_plan_state,
    _coverage_round_config,
    _account_shortage_reason,
    _canonicalized_task_config,
    _online_ready_accounts,
    requires_planning_with_open_actions,
    _select_accounts_for_plan,
)
from app.services.task_center.payloads import SendMessagePayload
from app.services.task_center import daily_coverage
from app.services.task_center import coverage_capacity
from app.services.task_center.daily_coverage_readiness import refresh_rows
from app.services.task_center.executors import group_ai_chat
from app.timezone import beijing_now


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


@pytest.fixture
def stable_capacity_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = beijing_now().replace(hour=20, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(sys.modules[__name__], "beijing_now", lambda: fixed_now)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: fixed_now)
    monkeypatch.setattr(coverage_capacity, "_now", lambda: fixed_now)


def _seed(session: Session) -> tuple[Task, TgGroup]:
    session.add(Tenant(id=1, name="租户"))
    session.add(AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True))
    group = TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群")
    task = Task(
        id="coverage-planner", tenant_id=1, name="覆盖 Planner", type="group_ai_chat", status="running",
        account_config={"selection_mode": "all", "max_concurrent": 10},
        type_config={
            "target_group_id": 21,
            "account_coverage_mode": "all_accounts_daily",
            "per_account_daily_min_messages": 1,
        },
    )
    session.add_all([group, task])
    for account_id in (1, 2, 3):
        session.add(TgAccount(
            id=account_id, tenant_id=1, pool_id=10, display_name=f"账号{account_id}",
            phone_masked=str(account_id), account_identity="normal", status="在线", health_score=95,
        ))
        session.add(TgGroupAccount(tenant_id=1, group_id=21, account_id=account_id, can_send=True))
    session.add_all([
        _coverage(task, 1, "ready"),
        _coverage(task, 2, "confirmed", confirmed_count=1),
        _coverage(task, 3, "blocked", blocker_code="account_limited"),
    ])
    session.commit()
    return task, group


def _coverage(
    task: Task,
    account_id: int,
    state: str,
    *,
    confirmed_count: int = 0,
    blocker_code: str = "",
) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id=f"coverage-{account_id}",
        tenant_id=1,
        task_id=task.id,
        group_id=21,
        account_id=account_id,
        coverage_date=beijing_now().date(),
        target_count=1,
        confirmed_count=confirmed_count,
        state=state,
        blocker_code=blocker_code,
    )


def test_all_account_planner_selects_only_ready_daily_ledger_accounts(session: Session) -> None:
    task, group = _seed(session)

    selected = _select_accounts_for_plan(
        session,
        task,
        group,
        {},
        task.type_config,
    )

    assert [account.id for account in selected] == [1]


def test_all_account_planner_does_not_fall_back_to_platform_scan_without_ready_debt(session: Session) -> None:
    task, group = _seed(session)
    session.get(TaskAccountDailyCoverage, "coverage-1").state = "confirmed"
    session.get(TaskAccountDailyCoverage, "coverage-1").confirmed_count = 1
    session.commit()

    selected = _select_accounts_for_plan(session, task, group, {}, task.type_config)

    assert selected == []


def test_running_all_account_task_blocks_when_daily_capacity_is_insufficient(
    session: Session,
    stable_capacity_clock: None,
) -> None:
    task, group = _seed(session)
    group.daily_limit = 1

    blocker = _coverage_capacity_blocker(session, task, group, task.type_config)

    assert blocker["blocker_code"] == "daily_coverage_capacity_insufficient"
    assert blocker["capacity_gap"] == 1
    assert task.stats["coverage_capacity_status"] == "blocked"


def test_running_all_account_task_clears_recovered_capacity_error(
    session: Session,
    stable_capacity_clock: None,
) -> None:
    task, group = _seed(session)
    task.last_error = "全部账号每日覆盖容量不足，已停止创建发送 Action"
    task.stats = {"coverage_capacity_status": "blocked"}

    blocker = _coverage_capacity_blocker(session, task, group, task.type_config)

    assert blocker == {}
    assert task.last_error == ""
    assert "coverage_capacity_status" not in task.stats


def test_offline_projection_is_written_to_account_coverage_blocker(session: Session) -> None:
    task, _group = _seed(session)
    account = session.get(TgAccount, 1)

    assert _online_ready_accounts(session, task, [account], {}) == []

    row = session.get(TaskAccountDailyCoverage, "coverage-1")
    assert row.state == "blocked"
    assert row.blocker_code == "account_offline"
    assert row.next_eligible_at is not None


def test_all_account_shortage_reason_does_not_scan_platform_accounts(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    task.stats = {"account_offline_count": 1}
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._has_account_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("platform scan")),
    )

    message, reason = _account_shortage_reason(session, task, group, {})

    assert reason == "account_offline"
    assert "在线" in message


def test_planner_normalizes_legacy_all_account_coverage_config(session: Session) -> None:
    task, _group = _seed(session)
    task.type_config = {**task.type_config, "account_coverage_mode": "natural"}

    config = _canonicalized_task_config(session, task, dict(task.type_config))

    assert config["account_coverage_mode"] == "all_accounts_daily"
    assert task.type_config["account_coverage_mode"] == "all_accounts_daily"


def test_coverage_plan_state_materializes_scope_once_and_reuses_rows(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    group.active_window = "00:00-23:59"
    calls = 0

    def count_ensure(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.ensure_task_daily_coverage",
        count_ensure,
    )

    state = _coverage_plan_state(session, task, group, task.type_config, {})

    assert calls == 1
    assert set(state.rows_by_account) == {1}
    assert state.account_count == 3


def test_coverage_plan_state_reconciles_remote_success_before_capacity_gate(
    session: Session,
    stable_capacity_clock: None,
) -> None:
    task, group = _seed(session)
    group.daily_limit = 3
    for account_id in (1, 2, 3):
        row = session.get(TaskAccountDailyCoverage, f"coverage-{account_id}")
        row.state = "ready"
        row.confirmed_count = 0
    action = Action(
        id="today-success",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=1,
        status="success",
        executed_at=beijing_now(),
        payload={"group_id": group.id},
    )
    session.add_all([
        action,
        ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=1,
            attempt_no=1,
            status="success",
            remote_message_id="tg-current-day",
        ),
    ])
    session.flush()

    state = _coverage_plan_state(session, task, group, task.type_config, {})

    assert state.confirmed_count == 1
    assert session.get(TaskAccountDailyCoverage, "coverage-1").state == "confirmed"
    assert _coverage_capacity_blocker(
        session, task, group, task.type_config, coverage_rows=state.rows, coverage_state=state,
    ) == {}


def test_account_selection_uses_supplied_coverage_snapshot_without_reread(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    row = session.get(TaskAccountDailyCoverage, "coverage-1")
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.ready_coverage_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ledger reread")),
    )

    selected = _select_accounts_for_plan(
        session,
        task,
        group,
        {},
        task.type_config,
        coverage_rows=[row],
    )

    assert [account.id for account in selected] == [1]


def test_coverage_round_does_not_repeat_one_account_for_multiple_obligations() -> None:
    config = {"account_coverage_mode": "all_accounts_daily", "allow_account_repeat": True}

    assert _coverage_round_config(config)["allow_account_repeat"] is False


def test_ready_coverage_plan_batch_uses_stable_cursor_and_hard_limit(session: Session) -> None:
    task, _group = _seed(session)
    session.query(TaskAccountDailyCoverage).delete()
    targeted_at = datetime(2026, 7, 13, 9, 0)
    session.add_all([
        TaskAccountDailyCoverage(
            id=f"coverage-batch-{index:02d}", tenant_id=1, task_id=task.id, group_id=21,
            account_id=100 + index, coverage_date=targeted_at.date(), target_count=1,
            state="ready", targeted_at=targeted_at,
        )
        for index in range(25)
    ])
    session.commit()

    first = daily_coverage.ready_coverage_plan_batch(session, task, now=targeted_at, limit=100)
    assert len(first.rows) == 20
    assert [row.account_id for row in first.rows] == list(range(100, 120))
    daily_coverage.advance_coverage_plan_cursor(session, task, first.rows[-1], now=targeted_at)
    for row in first.rows:
        row.state = "reserved"
    session.commit()

    second = daily_coverage.ready_coverage_plan_batch(session, task, now=targeted_at, limit=100)
    assert [row.account_id for row in second.rows] == list(range(120, 125))


def test_admission_ready_transition_requeues_row_after_cursor(session: Session) -> None:
    task, group = _seed(session)
    session.query(TaskAccountDailyCoverage).delete()
    initial_at = datetime(2026, 7, 13, 9, 0)
    cursor_at = datetime(2026, 7, 13, 9, 1)
    after_cursor_at = datetime(2026, 7, 13, 9, 2)
    admitted_at = datetime(2026, 7, 13, 9, 3)
    pending = TaskAccountDailyCoverage(
        id="coverage-pending", tenant_id=1, task_id=task.id, group_id=21,
        account_id=2, coverage_date=initial_at.date(), target_count=1,
        state="pending_admission", targeted_at=initial_at,
    )
    cursor_row = TaskAccountDailyCoverage(
        id="coverage-cursor", tenant_id=1, task_id=task.id, group_id=21,
        account_id=3, coverage_date=initial_at.date(), target_count=1,
        state="ready", targeted_at=cursor_at,
    )
    still_ready = TaskAccountDailyCoverage(
        id="coverage-still-ready", tenant_id=1, task_id=task.id, group_id=21,
        account_id=1, coverage_date=initial_at.date(), target_count=1,
        state="ready", targeted_at=after_cursor_at,
    )
    pending_membership = TaskMembershipAdmissionItem(
        tenant_id=1, task_id=task.id, account_id=2, target_id=21, phase="pending",
    )
    ready_membership = TaskMembershipAdmissionItem(
        tenant_id=1, task_id=task.id, account_id=1, target_id=21, phase="completed",
    )
    session.add_all([pending, cursor_row, still_ready, pending_membership, ready_membership])
    session.get(TgAccount, 1).session_ciphertext = encrypt_session("session-1")
    session.get(TgAccount, 2).session_ciphertext = encrypt_session("session-2")
    session.flush()
    daily_coverage.advance_coverage_plan_cursor(session, task, cursor_row, now=cursor_at)
    cursor_row.state = "reserved"
    session.commit()

    refresh_rows(session, [(pending, pending_membership), (still_ready, ready_membership)], group, admitted_at)

    assert pending.targeted_at == admitted_at
    assert still_ready.targeted_at == after_cursor_at
    batch = daily_coverage.ready_coverage_plan_batch(session, task, now=admitted_at, limit=20)
    assert [row.account_id for row in batch.rows] == [1, 2]


def test_online_recovery_requeues_row_after_cursor(session: Session) -> None:
    task, _group = _seed(session)
    session.query(TaskAccountDailyCoverage).delete()
    initial_at = datetime(2026, 7, 13, 10, 0)
    cursor_at = datetime(2026, 7, 13, 10, 1)
    after_cursor_at = datetime(2026, 7, 13, 10, 2)
    recovered_at = datetime(2026, 7, 13, 10, 3)
    offline = TaskAccountDailyCoverage(
        id="coverage-offline", tenant_id=1, task_id=task.id, group_id=21,
        account_id=1, coverage_date=initial_at.date(), target_count=1,
        state="blocked", blocker_code="account_offline", targeted_at=initial_at,
    )
    cursor_row = TaskAccountDailyCoverage(
        id="coverage-online-cursor", tenant_id=1, task_id=task.id, group_id=21,
        account_id=2, coverage_date=initial_at.date(), target_count=1,
        state="ready", targeted_at=cursor_at,
    )
    after_cursor = TaskAccountDailyCoverage(
        id="coverage-online-after", tenant_id=1, task_id=task.id, group_id=21,
        account_id=3, coverage_date=initial_at.date(), target_count=1,
        state="ready", targeted_at=after_cursor_at,
    )
    session.add_all([offline, cursor_row, after_cursor])
    session.flush()
    daily_coverage.advance_coverage_plan_cursor(session, task, cursor_row, now=cursor_at)
    cursor_row.state = "reserved"
    session.commit()

    assert daily_coverage.release_online_coverage_blockers(
        session, tenant_id=1, account_id=1, now=recovered_at,
    ) == 1
    assert offline.targeted_at == recovered_at
    batch = daily_coverage.ready_coverage_plan_batch(session, task, now=recovered_at, limit=20)
    assert [row.account_id for row in batch.rows] == [3, 1]


def test_coverage_plan_cursor_rolls_back_with_failed_batch(session: Session) -> None:
    task, _group = _seed(session)
    row = session.get(TaskAccountDailyCoverage, "coverage-1")
    timestamp = datetime(2026, 7, 13, 9, 0)
    row.coverage_date = timestamp.date()
    row.targeted_at = timestamp
    session.commit()

    batch = daily_coverage.ready_coverage_plan_batch(session, task, now=timestamp, limit=20)
    daily_coverage.advance_coverage_plan_cursor(session, task, batch.rows[-1], now=timestamp)
    session.rollback()

    retried = daily_coverage.ready_coverage_plan_batch(session, task, now=timestamp, limit=20)
    assert [item.id for item in retried.rows] == ["coverage-1"]


def test_daily_coverage_open_action_gate_uses_debt_after_reserved(monkeypatch, session: Session) -> None:
    task, group = _seed(session)
    group.active_window = "00:00-23:59"
    now_value = datetime.combine(beijing_now().date(), time(23, 59))
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    assert requires_planning_with_open_actions(session, task) is True

    session.get(TaskAccountDailyCoverage, "coverage-1").state = "reserved"
    session.get(TaskAccountDailyCoverage, "coverage-3").state = "reserved"

    assert requires_planning_with_open_actions(session, task) is False


def test_send_message_payload_carries_coverage_ledger_identity() -> None:
    payload = SendMessagePayload(
        group_id=21,
        message_text="自然生成的群聊内容",
        account_coverage_mode="all_accounts_daily",
        coverage_ledger_id="coverage-1",
    )

    assert payload.model_dump()["coverage_ledger_id"] == "coverage-1"
