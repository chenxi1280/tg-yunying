from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    Task,
    TaskDayLedger,
    Tenant,
)
from app.schemas.task_center import ChannelViewConfig
from app.services.task_center.channel_view_targets import _eligible_at
from app.services.task_center.executors.channel_view import (
    _message_expired,
    adjust_for_account_view_spacing,
)
from app.services.task_center.executors.channel_view_pacing import (
    effective_channel_view_pacing_config,
)
from app.services.task_center.pacing import schedule_due_times
from app.timezone import as_beijing, beijing_now

pytestmark = pytest.mark.no_postgres


def _setup_sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    tenant = Tenant(id=1, name="Test Tenant")
    session.add(tenant)
    session.commit()
    return session


def test_channel_view_config_default_message_active_days_is_seven():
    cfg = ChannelViewConfig(target_channel_id=1)
    assert cfg.message_active_days == 7


def test_channel_view_message_active_days_seven_days_window():
    now = beijing_now()
    session = _setup_sqlite_session()
    try:
        msg_6d_ago = ChannelMessage(
            id=1,
            tenant_id=1,
            channel_target_id=10,
            message_id=101,
            published_at=(now - timedelta(days=6)).replace(tzinfo=None),
        )
        msg_8d_ago = ChannelMessage(
            id=2,
            tenant_id=1,
            channel_target_id=10,
            message_id=102,
            published_at=(now - timedelta(days=8)).replace(tzinfo=None),
        )
        session.add_all([msg_6d_ago, msg_8d_ago])
        session.commit()

        # With default or 0 active days, defaults to 7 days
        assert not _message_expired(msg_6d_ago, {})
        assert _message_expired(msg_8d_ago, {})

        assert _eligible_at(msg_6d_ago, {}, now) is True
        assert _eligible_at(msg_8d_ago, {}, now) is False
    finally:
        session.close()


def test_effective_channel_view_pacing_distributes_across_twenty_four_hours():
    task = Task(
        id="task-view-pacing-test",
        tenant_id=1,
        type="channel_view",
        timezone="Asia/Shanghai",
        pacing_config={"mode": "template"},
    )
    pacing_config = effective_channel_view_pacing_config(task)
    curve = pacing_config.get("operation_profile", {}).get("hourly_activity_curve")
    assert curve == [1] * 24

    # Schedule 24 actions across the full day
    start_at = datetime(2026, 9, 3, 0, 0, 0)
    deadline_at = datetime(2026, 9, 3, 23, 59, 59)
    times = schedule_due_times(
        24,
        pacing_config,
        period_start_at=start_at,
        deadline_at=deadline_at,
        seed_id="test:seed",
    )
    assert len(times) == 24
    # All 24 hours should have samples
    hours = {t.hour for t in times}
    assert len(hours) >= 20  # Stratified random covers virtually all hours


def test_adjust_for_account_view_spacing_per_account_twelve_hours():
    session = _setup_sqlite_session()
    try:
        task = Task(
            id="task-spacing-1",
            name="Test Channel View Task",
            tenant_id=1,
            type="channel_view",
            timezone="Asia/Shanghai",
        )
        session.add(task)

        # Day 2 ledger
        day2_date = datetime(2026, 9, 3).date()
        ledger_day2 = TaskDayLedger(
            id=2,
            tenant_id=1,
            task_id=task.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            day_phase="planning",
            obligation_local_date=day2_date,
            period_start_at=datetime(2026, 9, 3, 0, 0, 0),
            planning_anchor_at=datetime(2026, 9, 3, 0, 0, 0),
            deadline_at=datetime(2026, 9, 3, 23, 59, 59),
        )
        session.add(ledger_day2)

        # Account 101: executed yesterday at 22:00
        account_101_executed_yesterday = datetime(2026, 9, 2, 22, 0, 0)
        action_101 = Action(
            tenant_id=1,
            task_id=task.id,
            account_id=101,
            task_type="channel_view",
            action_type="view_message",
            status="success",
            scheduled_at=account_101_executed_yesterday,
            executed_at=account_101_executed_yesterday,
            payload={},
        )

        # Account 102: executed yesterday at 08:00
        account_102_executed_yesterday = datetime(2026, 9, 2, 8, 0, 0)
        action_102 = Action(
            tenant_id=1,
            task_id=task.id,
            account_id=102,
            task_type="channel_view",
            action_type="view_message",
            status="success",
            scheduled_at=account_102_executed_yesterday,
            executed_at=account_102_executed_yesterday,
            payload={},
        )
        session.add_all([action_101, action_102])
        session.commit()

        # Both accounts have today's initial scheduled time at 04:00 AM today
        today_early_schedule = datetime(2026, 9, 3, 4, 0, 0)

        # Account 101 must be shifted because 22:00 + 12h = 10:00 AM today
        adjusted_101 = adjust_for_account_view_spacing(
            session,
            task,
            account_id=101,
            scheduled_at=today_early_schedule,
            deadline_at=ledger_day2.deadline_at,
            ledger=ledger_day2,
            min_spacing_hours=12,
        )
        # Should be at or after 10:00 AM today
        assert as_beijing(adjusted_101) >= as_beijing(datetime(2026, 9, 3, 10, 0, 0))
        # Spacing from previous execution is >= 12h
        assert (as_beijing(adjusted_101) - as_beijing(account_101_executed_yesterday)).total_seconds() >= 12 * 3600

        # Account 102 should NOT be shifted because 08:00 + 12h = 20:00 yesterday, so 04:00 today is already 20h later
        adjusted_102 = adjust_for_account_view_spacing(
            session,
            task,
            account_id=102,
            scheduled_at=today_early_schedule,
            deadline_at=ledger_day2.deadline_at,
            ledger=ledger_day2,
            min_spacing_hours=12,
        )
        assert adjusted_102 == today_early_schedule

        # Account 103: New account with no previous executions, should not be shifted
        adjusted_103 = adjust_for_account_view_spacing(
            session,
            task,
            account_id=103,
            scheduled_at=today_early_schedule,
            deadline_at=ledger_day2.deadline_at,
            ledger=ledger_day2,
            min_spacing_hours=12,
        )
        assert adjusted_103 == today_early_schedule
    finally:
        session.close()


def test_channel_view_uses_existing_messages_when_snapshot_collecting():
    from app.models import OperationTarget, TaskSourceSubscription, ListenerSourceState
    from app.services.task_center.executors.common import channel_scope
    from app.services.task_center.channel_listener_runtime import ensure_channel_subscription

    session = _setup_sqlite_session()
    try:
        now = beijing_now()
        channel = OperationTarget(
            id=20,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="peer20",
            title="Test Channel",
            username="testchan",
        )
        task = Task(
            id="task-view-fallback",
            tenant_id=1,
            type="channel_view",
            name="Fallback Task",
            timezone="Asia/Shanghai",
            type_config={"target_channel_id": 20, "message_active_days": 7},
        )
        state = ListenerSourceState(
            id="state-collecting-1",
            tenant_id=1,
            source_type="channel",
            source_peer_id="peer20",
            snapshot_status="collecting",
            snapshot_revision=1,
        )
        session.add_all([channel, task, state])
        session.commit()

        sub = ensure_channel_subscription(session, task, channel)
        sub.listener_source_state_id = state.id
        msg = ChannelMessage(
            id=201,
            tenant_id=1,
            channel_target_id=20,
            message_id=501,
            published_at=(now - timedelta(days=2)).replace(tzinfo=None),
        )
        session.add(msg)
        session.commit()

        # channel_scope should not starve even though snapshot_status is collecting
        scoped_channel, messages = channel_scope(session, task, dict(task.type_config or {}))
        assert scoped_channel is not None
        assert len(messages) >= 1
        assert messages[0].id == 201
    finally:
        session.close()

