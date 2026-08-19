from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ChannelMessage,
    OperationTarget,
    Task,
    TaskDayLedger,
    Tenant,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.channel_view_targets import (
    channel_view_target_due,
    ensure_channel_view_targets,
    target_messages,
)
from app.services.task_center.executors import channel_view


pytestmark = pytest.mark.no_postgres


def test_daily_target_freezes_source_and_quantity_across_config_change() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 8, 10, 10, 0)
    with Session(engine) as session:
        task, channel, message, ledger = _scope(now_value)
        session.add_all([Tenant(id=1, name="单用户"), task, channel, message, ledger])
        session.flush()

        first = ensure_channel_view_targets(
            session,
            task,
            channel,
            ledger=ledger,
            messages=[message],
            config=_config(10),
            now=now_value,
        )[message.id]
        second = ensure_channel_view_targets(
            session,
            task,
            channel,
            ledger=ledger,
            messages=[],
            config=_config(1),
            now=now_value + timedelta(hours=1),
        )[message.id]

        assert first.id == second.id
        assert second.daily_target_snapshot == 10
        assert [row.id for row in target_messages(session, {message.id: second})] == [message.id]
        assert channel_view_target_due(second, ledger, {}, now=ledger.deadline_at) == 6


def test_current_daily_due_does_not_subtract_lifetime_account_identities() -> None:
    now_value = datetime(2026, 8, 11, 0, 0)
    _task, _channel, message, ledger = _scope(now_value)
    target = SimpleNamespace(
        daily_target_snapshot=10,
        total_target_snapshot=100,
        effective_target_snapshot=10,
        ledger_confirmed_at_attach=0,
        accrual_anchor_at=ledger.period_start_at,
        active_until=ledger.deadline_at,
    )
    inputs = SimpleNamespace(
        ledger=ledger,
        now=ledger.deadline_at,
        targets_by_message={message.id: target},
        materialized_ids_by_message={message.id: set()},
        lifetime_ids_by_message={message.id: set(range(10))},
    )

    quantity = channel_view._view_quantity_for_message(
        _task,
        inputs,
        message,
        config={},
    )

    assert quantity == 10


def test_fixed_zero_interval_target_is_due_immediately() -> None:
    now_value = datetime(2026, 8, 11, 10, 0)
    _task, _channel, _message, ledger = _scope(now_value)
    target = SimpleNamespace(
        effective_target_snapshot=3,
        accrual_anchor_at=now_value,
        active_until=ledger.deadline_at,
    )

    due = channel_view_target_due(
        target,
        ledger,
        {
            "mode": "fixed",
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
        },
        now=now_value,
    )

    assert due == 3


def test_target_attach_freezes_global_and_current_ledger_fact_baselines() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 8, 10, 10, 0)
    with Session(engine) as session:
        task, channel, message, ledger = _scope(now_value)
        other_task, other_ledger = _other_scope(now_value)
        session.add_all([
            Tenant(id=1, name="单用户"), task, other_task, channel, message, ledger, other_ledger,
        ])
        session.flush()
        current = _view_fact(ledger, message, account_id=11, confirmed_at=now_value)
        external = _view_fact(other_ledger, message, account_id=12, confirmed_at=now_value)
        session.add_all([current[0], external[0]])
        session.flush()
        session.add_all([current[1], external[1]])
        session.flush()

        target = ensure_channel_view_targets(
            session,
            task,
            channel,
            ledger=ledger,
            messages=[message],
            config={
                "per_message_daily_view_target": 10,
                "per_message_total_view_target": 11,
                "message_active_days": 3,
            },
            now=now_value,
        )[message.id]

        assert target.lifetime_confirmed_at_attach == 2
        assert target.ledger_confirmed_at_attach == 1
        assert target.effective_target_snapshot == 9


def test_account_scan_limit_includes_lifetime_exclusions(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_select(*_args, **kwargs):
        captured["limit"] = int(kwargs["limit"])
        return []

    monkeypatch.setattr(channel_view, "select_task_accounts", fake_select)
    monkeypatch.setattr(channel_view, "channel_member_accounts", lambda *_args: [])
    task = SimpleNamespace(account_config={}, tenant_id=1, id="task")

    channel_view._view_accounts(
        None,
        task,
        SimpleNamespace(),
        config={},
        target_per_message=10,
        identity_scan_floor=20,
    )

    assert captured["limit"] == 20


def test_unique_account_capacity_shortfall_is_persisted_without_reusing_identity() -> None:
    now_value = datetime(2026, 8, 11, 0, 0)
    task, _channel, message, ledger = _scope(now_value)
    target = SimpleNamespace(
        effective_target_snapshot=2,
        accrual_anchor_at=now_value,
        active_until=ledger.deadline_at,
        ledger_confirmed_at_attach=0,
    )
    inputs = SimpleNamespace(
        messages=[message],
        accounts=[SimpleNamespace(id=11), SimpleNamespace(id=12)],
        daily_counts_by_account={},
        ledger=ledger,
        targets_by_message={message.id: target},
        lifetime_ids_by_message={message.id: {11, 12}},
        materialized_ids_by_message={message.id: set()},
        now=ledger.deadline_at,
    )

    assert channel_view._record_unique_capacity(task, inputs, config={}) is True
    assert task.stats["channel_view_unique_account_capacity_shortfall"] == {
        "source_count": 1,
        "required_count": 2,
        "available_count": 0,
        "deficit_count": 2,
    }
    task.last_error = "channel_view_unique_account_capacity_shortfall"

    assert channel_view._record_unique_capacity(
        task,
        SimpleNamespace(**{**inputs.__dict__, "lifetime_ids_by_message": {message.id: set()}}),
        config={},
    ) is False
    assert task.last_error == ""
    assert "channel_view_unique_account_capacity_shortfall" not in task.stats


def _scope(
    now_value: datetime,
) -> tuple[Task, OperationTarget, ChannelMessage, TaskDayLedger]:
    task = Task(
        id="view-target-task",
        tenant_id=1,
        name="view",
        type="channel_view",
        status="running",
        created_at=now_value - timedelta(days=1),
    )
    channel = OperationTarget(
        id=901,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-100901",
        title="频道",
    )
    message = ChannelMessage(
        id=902,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=1902,
        published_at=now_value - timedelta(hours=1),
        created_at=now_value - timedelta(hours=1),
    )
    ledger = TaskDayLedger(
        id="view-target-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 10),
        period_start_at=now_value.replace(hour=0),
        deadline_at=now_value.replace(hour=0) + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=now_value.replace(hour=0),
    )
    return task, channel, message, ledger


def _config(target: int) -> dict:
    return {
        "per_message_daily_view_target": target,
        "per_message_total_view_target": target,
        "message_active_days": 3,
    }


def _other_scope(now_value: datetime) -> tuple[Task, TaskDayLedger]:
    task = Task(
        id="other-view-task",
        tenant_id=1,
        name="other-view",
        type="channel_view",
        status="running",
        created_at=now_value - timedelta(days=2),
    )
    ledger = TaskDayLedger(
        id="other-view-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 10),
        period_start_at=now_value.replace(hour=0),
        deadline_at=now_value.replace(hour=0) + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=now_value.replace(hour=0),
    )
    return task, ledger


def _view_fact(
    ledger: TaskDayLedger,
    message: ChannelMessage,
    *,
    account_id: int,
    confirmed_at: datetime,
) -> tuple[ViewFulfillmentObligation, ViewRemoteFact]:
    obligation = ViewFulfillmentObligation(
        id=f"obligation-{ledger.id}-{account_id}",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=account_id,
        status="confirmed",
    )
    fact = ViewRemoteFact(
        id=f"fact-{ledger.id}-{account_id}",
        tenant_id=1,
        obligation_id=obligation.id,
        target_peer_id="-100901",
        channel_message_id=message.id,
        account_id=account_id,
        remote_confirmed_at=confirmed_at,
    )
    return obligation, fact
