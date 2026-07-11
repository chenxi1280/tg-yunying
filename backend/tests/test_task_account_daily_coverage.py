from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    Action,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.security import encrypt_session
from app.services.task_center.account_scope import initialize_all_account_task_scope, sync_account_to_all_tasks
from app.services.task_center.daily_coverage import (
    daily_coverage_due_debt,
    ensure_task_daily_coverage,
    ready_coverage_rows,
    release_coverage_reservation,
    reserve_coverage_for_action,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed(session: Session) -> Task:
    session.add(Tenant(id=1, name="租户"))
    session.add(AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群", active_window="09:00-23:00"))
    session.add(OperationTarget(
        id=31, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群",
        auth_status="已授权运营", can_send=True,
    ))
    task = Task(
        id="coverage-task", tenant_id=1, name="覆盖任务", type="group_ai_chat", status="running",
        account_config={"selection_mode": "all"},
        type_config={
            "target_group_id": 21,
            "target_operation_target_id": 31,
            "account_coverage_mode": "all_accounts_daily",
            "per_account_daily_min_messages": 1,
        },
    )
    session.add(task)
    return task


def _account(account_id: int, *, status: str = "在线") -> TgAccount:
    return TgAccount(
        id=account_id, tenant_id=1, pool_id=10, display_name=f"账号{account_id}",
        phone_masked=f"***{account_id:04d}", account_identity="normal", status=status,
        session_ciphertext=encrypt_session(f"session-{account_id}"),
    )


def test_daily_ledger_keeps_ready_pending_and_blocked_accounts_in_denominator(session: Session) -> None:
    task = _seed(session)
    session.add_all([_account(1), _account(2), _account(3)])
    session.add_all([
        TgGroupAccount(tenant_id=1, group_id=21, account_id=1, can_send=True),
        TgGroupAccount(tenant_id=1, group_id=21, account_id=3, can_send=False),
    ])
    session.commit()

    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))
    ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 10))
    session.commit()

    rows = list(session.scalars(select(TaskAccountDailyCoverage).order_by(TaskAccountDailyCoverage.account_id)))
    assert [(row.account_id, row.state, row.blocker_code) for row in rows] == [
        (1, "ready", ""),
        (2, "pending_admission", "not_in_group"),
        (3, "blocked", "cannot_send"),
    ]
    assert all(row.coverage_date == date(2026, 7, 10) for row in rows)


def test_account_state_change_does_not_remove_existing_daily_obligation(session: Session) -> None:
    task = _seed(session)
    account = _account(1)
    session.add(account)
    session.add(TgGroupAccount(tenant_id=1, group_id=21, account_id=1, can_send=True))
    session.commit()
    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))
    ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 10))
    account.status = "Session失效"
    session.commit()

    sync_account_to_all_tasks(session, account.id, now=datetime(2026, 7, 10, 12))
    session.commit()

    rows = list(session.scalars(select(TaskAccountDailyCoverage)))
    assert len(rows) == 1
    assert rows[0].account_id == 1
    assert rows[0].state == "blocked"
    assert rows[0].blocker_code == "session_expired"


def test_account_becoming_ready_after_active_window_starts_next_day(session: Session) -> None:
    task = _seed(session)
    session.add(_account(1))
    session.commit()

    assert sync_account_to_all_tasks(session, 1, now=datetime(2026, 7, 10, 23, 30)) == 1
    session.commit()

    row = session.scalar(select(TaskAccountDailyCoverage))
    assert row is not None
    assert row.coverage_date == date(2026, 7, 11)


def test_reservation_is_bound_to_one_action_and_can_be_released(session: Session) -> None:
    task = _seed(session)
    session.add(_account(1))
    session.add(TgGroupAccount(tenant_id=1, group_id=21, account_id=1, can_send=True))
    session.commit()
    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))
    ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 10))
    first = Action(id="action-1", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", account_id=1)
    second = Action(id="action-2", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", account_id=1)
    session.add_all([first, second])
    session.flush()
    row = session.scalar(select(TaskAccountDailyCoverage))

    assert reserve_coverage_for_action(session, row.id, first.id, now=datetime(2026, 7, 10, 10, 1)) is True
    assert reserve_coverage_for_action(session, row.id, second.id, now=datetime(2026, 7, 10, 10, 2)) is False
    assert release_coverage_reservation(session, row.id, first.id, blocker_code="duplicate_message") is True
    session.refresh(row)
    assert row.state == "ready"
    assert row.reserved_action_id is None
    assert row.blocker_code == "duplicate_message"


def test_daily_ledger_creation_is_idempotent(session: Session) -> None:
    task = _seed(session)
    session.add(_account(1))
    session.commit()
    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))

    first = ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 10))
    second = ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 11))
    session.commit()

    assert first.created == 0
    assert second.created == 0
    assert len(list(session.scalars(select(TaskAccountDailyCoverage)))) == 1
    assert len(list(session.scalars(select(TaskMembershipAdmissionItem)))) == 1


def test_existing_complete_daily_scope_skips_per_account_readiness_refresh(session: Session, monkeypatch) -> None:
    task = _seed(session)
    session.add(_account(1))
    session.commit()
    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))
    monkeypatch.setattr(
        "app.services.task_center.daily_coverage.refresh_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected refresh")),
    )

    result = ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 10, 11))

    assert result.created == 0
    assert result.refreshed == 0


def test_new_daily_scope_uses_batched_readiness_without_per_account_queries(session: Session) -> None:
    task = _seed(session)
    for account_id in range(1, 21):
        session.add(_account(account_id))
    session.commit()
    initialize_all_account_task_scope(session, task, now=datetime(2026, 7, 10, 10))
    statement_count = 0

    def count_select(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal statement_count
        if statement.lstrip().upper().startswith("SELECT"):
            statement_count += 1

    event.listen(session.bind, "before_cursor_execute", count_select)

    result = ensure_task_daily_coverage(session, task, now=datetime(2026, 7, 11, 10))

    event.remove(session.bind, "before_cursor_execute", count_select)

    assert result.created == 20
    assert result.refreshed == 20
    assert statement_count <= 7


def test_ready_coverage_rows_is_indexed_read_without_implicit_scope_refresh(session: Session, monkeypatch) -> None:
    task = _seed(session)
    for account_id in (1, 2):
        session.add(_account(account_id))
        session.add(TaskMembershipAdmissionItem(
            tenant_id=1,
            task_id=task.id,
            account_id=account_id,
            target_id=31,
            phase="completed",
        ))
        session.add(TaskAccountDailyCoverage(
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account_id,
            coverage_date=date(2026, 7, 10),
            state="ready",
        ))
    session.commit()
    monkeypatch.setattr(
        "app.services.task_center.daily_coverage.ensure_task_daily_coverage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("implicit refresh")),
    )

    rows = ready_coverage_rows(session, task, now=datetime(2026, 7, 10, 12), limit=1)

    assert [row.account_id for row in rows] == [1]


def test_daily_coverage_due_debt_uses_elapsed_active_window(session: Session) -> None:
    task = _seed(session)
    group = session.get(TgGroup, 21)
    rows = [
        TaskAccountDailyCoverage(
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account_id,
            coverage_date=date(2026, 7, 10),
            state="ready",
        )
        for account_id in range(1, 15)
    ]

    assert daily_coverage_due_debt(task, group, rows, now=datetime(2026, 7, 10, 8, 59)) == 0
    assert daily_coverage_due_debt(task, group, rows, now=datetime(2026, 7, 10, 10, 0)) == 1
    assert daily_coverage_due_debt(task, group, rows, now=datetime(2026, 7, 10, 16, 0)) == 7
    assert daily_coverage_due_debt(task, group, rows, now=datetime(2026, 7, 10, 23, 0)) == 14


def test_daily_coverage_due_debt_subtracts_confirmed_and_reserved(session: Session) -> None:
    task = _seed(session)
    group = session.get(TgGroup, 21)
    rows = [
        TaskAccountDailyCoverage(
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=1,
            coverage_date=date(2026, 7, 10),
            confirmed_count=1,
            state="confirmed",
        ),
        TaskAccountDailyCoverage(
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=2,
            coverage_date=date(2026, 7, 10),
            state="reserved",
        ),
        *[
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=21,
                account_id=account_id,
                coverage_date=date(2026, 7, 10),
                state="ready",
            )
            for account_id in range(3, 15)
        ],
    ]

    assert daily_coverage_due_debt(task, group, rows, now=datetime(2026, 7, 10, 11, 0)) == 0
