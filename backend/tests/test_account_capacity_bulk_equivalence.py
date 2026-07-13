from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, MessageTask, SchedulingSetting, Task, Tenant, TgAccount
from app.services.account_capacity import (
    ACTION_OCCUPIED_STATUSES,
    MESSAGE_TASK_OCCUPIED_STATUSES,
    AccountCapacityCache,
    AccountCapacityReservation,
    account_capacity_decision,
    available_accounts_by_capacity,
    next_capacity_window,
)


pytestmark = pytest.mark.no_postgres


def _session(*, cooldown: int = 120, hour_limit: int = 2, day_limit: int = 3) -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="default"))
    session.add(Task(id="capacity-task", tenant_id=1, name="capacity", type="group_ai_chat"))
    session.add(
        SchedulingSetting(
            tenant_id=1,
            default_account_cooldown_seconds=cooldown,
            default_account_hour_limit=hour_limit,
            default_account_day_limit=day_limit,
            jitter_min_seconds=0,
            jitter_max_seconds=0,
        )
    )
    session.commit()
    return session


def _account(session: Session, account_id: int) -> TgAccount:
    account = TgAccount(id=account_id, tenant_id=1, display_name=str(account_id), phone_masked=str(account_id))
    session.add(account)
    session.flush()
    return account


def _action(
    account_id: int,
    action_id: str,
    status: str,
    *,
    scheduled_at: datetime,
    executed_at: datetime | None = None,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="capacity-task",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status=status,
        scheduled_at=scheduled_at,
        executed_at=executed_at,
    )


def _message(
    account_id: int,
    message_id: int,
    status: str,
    *,
    scheduled_at: datetime,
    sent_at: datetime | None = None,
) -> MessageTask:
    return MessageTask(
        id=message_id,
        tenant_id=1,
        account_id=account_id,
        content=str(message_id),
        idempotency_key=f"capacity-{message_id}",
        status=status,
        scheduled_at=scheduled_at,
        sent_at=sent_at,
    )


def _assert_cached_matches(
    session: Session,
    accounts: list[TgAccount],
    scheduled_at: datetime,
    **options,
) -> AccountCapacityCache:
    uncached = {
        account.id: account_capacity_decision(
            session,
            tenant_id=1,
            account_id=account.id,
            scheduled_at=scheduled_at,
            **options,
        )
        for account in accounts
    }
    cache = AccountCapacityCache()
    available_accounts_by_capacity(
        session,
        tenant_id=1,
        accounts=accounts,
        scheduled_at=scheduled_at,
        cache=cache,
        **options,
    )
    cached = {
        account.id: account_capacity_decision(
            session,
            tenant_id=1,
            account_id=account.id,
            scheduled_at=scheduled_at,
            cache=cache,
            **options,
        )
        for account in accounts
    }
    assert cached == uncached
    return cache


@pytest.mark.parametrize("status", sorted(ACTION_OCCUPIED_STATUSES | {"failed", "skipped"}))
def test_bulk_cache_matches_action_status_and_executed_at(status: str) -> None:
    at = datetime(2026, 7, 13, 12, 30)
    with _session(cooldown=0, hour_limit=1, day_limit=0) as session:
        account = _account(session, 101)
        session.add(_action(101, f"action-{status}", status, scheduled_at=at - timedelta(days=1), executed_at=at - timedelta(minutes=1)))
        session.commit()

        _assert_cached_matches(session, [account], at)
        decision = account_capacity_decision(session, tenant_id=1, account_id=101, scheduled_at=at)

    assert decision.available is (status not in ACTION_OCCUPIED_STATUSES)


@pytest.mark.parametrize("status", sorted(MESSAGE_TASK_OCCUPIED_STATUSES | {"失败", "已取消"}))
def test_bulk_cache_matches_message_status_and_sent_at(status: str) -> None:
    at = datetime(2026, 7, 13, 12, 30)
    with _session(cooldown=0, hour_limit=1, day_limit=0) as session:
        account = _account(session, 101)
        session.add(_message(101, 1, status, scheduled_at=at - timedelta(days=1), sent_at=at - timedelta(minutes=1)))
        session.commit()

        _assert_cached_matches(session, [account], at)
        decision = account_capacity_decision(session, tenant_id=1, account_id=101, scheduled_at=at)

    assert decision.available is (status not in MESSAGE_TASK_OCCUPIED_STATUSES)


def test_bulk_cache_matches_hour_day_and_bidirectional_cooldown() -> None:
    at = datetime(2026, 7, 13, 12, 30)
    with _session() as session:
        accounts = [_account(session, account_id) for account_id in range(101, 106)]
        session.add_all(
            [
                _action(101, "hour-1", "success", scheduled_at=at - timedelta(minutes=20)),
                _action(101, "hour-2", "pending", scheduled_at=at - timedelta(minutes=10)),
                _action(102, "day-1", "success", scheduled_at=at - timedelta(hours=3)),
                _action(102, "day-2", "success", scheduled_at=at - timedelta(hours=2)),
                _action(102, "day-3", "success", scheduled_at=at - timedelta(hours=1)),
                _action(103, "cooldown-past", "success", scheduled_at=at - timedelta(seconds=30)),
                _action(104, "cooldown-future", "pending", scheduled_at=at + timedelta(seconds=30)),
            ]
        )
        session.commit()

        _assert_cached_matches(session, accounts, at)
        decisions = {
            account.id: account_capacity_decision(session, tenant_id=1, account_id=account.id, scheduled_at=at)
            for account in accounts
        }

    assert decisions[101].reason_code == "account_hour_limit"
    assert decisions[102].reason_code == "account_day_limit"
    assert decisions[103].reason_code == "account_cooldown"
    assert decisions[104].reason_code == "account_cooldown"
    assert decisions[105].available is True


def test_bulk_cache_matches_exclusions_reservations_and_next_window() -> None:
    at = datetime(2026, 7, 13, 12, 30)
    reservations = [AccountCapacityReservation(account_id=102, scheduled_at=at - timedelta(seconds=30))]
    with _session(cooldown=120, hour_limit=0, day_limit=0) as session:
        accounts = [_account(session, 101), _account(session, 102)]
        session.add(_action(101, "excluded-action", "success", scheduled_at=at - timedelta(seconds=30)))
        message = _message(102, 1, "排队中", scheduled_at=at - timedelta(seconds=30))
        session.add(message)
        session.commit()
        options = {
            "exclude_action_ids": {"excluded-action"},
            "exclude_message_task_id": message.id,
            "reservations": reservations,
        }
        cache = _assert_cached_matches(session, accounts, at, **options)
        uncached = next_capacity_window(session, tenant_id=1, account_ids=[101, 102], scheduled_at=at, **options)
        cached = next_capacity_window(session, tenant_id=1, account_ids=[101, 102], scheduled_at=at, cache=cache, **options)

    assert cached == uncached
    assert cached.defer_until == at + timedelta(seconds=90)


def test_bulk_cache_reprime_matches_across_hour_and_beijing_day() -> None:
    before_midnight = datetime(2026, 7, 13, 23, 59, 30)
    after_midnight = datetime(2026, 7, 14, 0, 0, 30)
    with _session(cooldown=120, hour_limit=1, day_limit=1) as session:
        account = _account(session, 101)
        session.add(_action(101, "before-midnight", "success", scheduled_at=before_midnight - timedelta(seconds=30)))
        session.commit()
        cache = _assert_cached_matches(session, [account], before_midnight)
        available_accounts_by_capacity(session, tenant_id=1, accounts=[account], scheduled_at=after_midnight, cache=cache)
        cached = account_capacity_decision(session, tenant_id=1, account_id=101, scheduled_at=after_midnight, cache=cache)
        uncached = account_capacity_decision(session, tenant_id=1, account_id=101, scheduled_at=after_midnight)

    assert cached == uncached
    assert cached.reason_code == "account_cooldown"
