from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import Task
from app.services.task_center.executors import channel_view, group_ai_chat
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.pacing import (
    minimum_schedule_gap_seconds,
    schedule_due_times,
    schedule_times,
)
from app.services.task_center.schedule_reservation import (
    continue_schedule_after,
    reserve_task_schedule_times,
)


pytestmark = pytest.mark.no_postgres


def test_repeated_template_batch_continues_after_latest_open_action() -> None:
    latest = datetime(2026, 8, 8, 18, 0)
    session = Mock()
    session.scalar.return_value = latest
    task = SimpleNamespace(id="paced-task")

    reserved = reserve_task_schedule_times(
        session,
        task,
        "view_message",
        [latest, latest + timedelta(minutes=5)],
        pacing_config={"mode": "template", "template": "moderate_6h"},
    )

    assert reserved == [
        latest + timedelta(minutes=3),
        latest + timedelta(minutes=8),
    ]


def test_completed_batch_keeps_success_execution_as_pacing_anchor() -> None:
    success_at = datetime(2026, 8, 8, 18, 0)
    session = Mock()
    session.scalar.side_effect = [None, success_at]
    task = SimpleNamespace(id="completed-paced-task")

    reserved = reserve_task_schedule_times(
        session,
        task,
        "send_message",
        [success_at + timedelta(minutes=1)],
        pacing_config={"mode": "template", "template": "moderate_6h"},
    )

    assert reserved == [success_at + timedelta(minutes=3)]


def test_schedule_reservation_drops_rows_that_do_not_fit_deadline() -> None:
    start = datetime(2026, 8, 8, 23, 57)

    reserved = continue_schedule_after(
        [start, start + timedelta(minutes=3)],
        latest_scheduled_at=start,
        minimum_gap_seconds=180,
        deadline_at=start + timedelta(minutes=4),
    )

    assert reserved == [start + timedelta(minutes=3)]


def test_channel_view_schedule_does_not_anchor_after_latest_future_action() -> None:
    now_value = datetime(2026, 8, 10, 10, 0)
    deadline = datetime(2026, 8, 10, 23, 59, 59)
    session = Mock()
    task = SimpleNamespace(id="channel-view-gap-merge")

    reserved = reserve_task_schedule_times(
        session,
        task,
        "view_message",
        [now_value, now_value + timedelta(seconds=30), deadline],
        pacing_config={"mode": "template", "template": "moderate_6h"},
        deadline_at=deadline,
        enforce_task_spacing=False,
    )

    assert reserved == [now_value, now_value + timedelta(seconds=30)]
    session.scalar.assert_not_called()


def test_channel_view_curve_is_not_capped_by_task_level_template_gap() -> None:
    start = datetime(2026, 8, 10, 10, 0)
    deadline = start + timedelta(minutes=10)

    planned = schedule_times(
        100,
        {"mode": "template", "template": "moderate_6h"},
        start_at=start,
        deadline_at=deadline,
        preserve_minimum_spacing=False,
    )

    assert len(planned) == 100
    assert min(planned) >= start
    assert max(planned) < deadline


def test_current_due_schedule_randomizes_within_hour_buckets() -> None:
    start = datetime(2026, 8, 11, 12, 0)
    deadline = datetime(2026, 8, 12, 0, 0)

    planned = schedule_due_times(
        100,
        {"mode": "template", "template": "moderate_6h", "max_actions_per_hour": 1},
        start_at=start,
        deadline_at=deadline,
        timezone_name="Asia/Shanghai",
    )

    assert len(planned) == 100
    assert min(planned) >= start
    assert max(planned) < deadline
    # 小时桶随机分布：不再是同一时刻，也不等距
    ordered = sorted(planned)
    gaps = {
        (later - earlier).total_seconds()
        for earlier, later in zip(ordered, ordered[1:])
    }
    assert len(gaps) > 1


def test_current_ai_and_view_use_due_schedule_instead_of_second_pacing(
    monkeypatch,
) -> None:
    start = datetime(2026, 8, 11, 12, 0)
    deadline = datetime(2026, 8, 12, 0, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: start)
    monkeypatch.setattr(channel_view, "_now", lambda: start)
    task = SimpleNamespace(
        id="current-due",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
        pacing_config={"mode": "template", "template": "moderate_6h"},
        timezone="Asia/Shanghai",
    )
    session = Mock()

    ai_times = group_ai_chat._schedule_times_for_plan(
        session,
        task,
        {},
        3,
        mode="正常期",
        deadline_at=deadline,
    )
    view_times = channel_view._view_schedule_times(
        session,
        task,
        3,
        deadline_at=deadline,
    )

    for times in (ai_times, view_times):
        assert len(times) == 3
        assert min(times) >= start
        assert max(times) < deadline
    session.scalar.assert_not_called()


def test_due_schedule_drops_quiet_hour_shift_at_half_open_deadline() -> None:
    start = datetime(2026, 8, 11, 23, 30)
    deadline = datetime(2026, 8, 12, 0, 0)

    planned = schedule_due_times(
        2,
        {"quiet_hours": {"start": "23:00", "end": "08:00"}},
        start_at=start,
        deadline_at=deadline,
        timezone_name="Asia/Shanghai",
    )

    assert planned == []


def test_due_schedule_compares_naive_utc_ledger_deadline_as_beijing_wall_time() -> None:
    start = datetime(2026, 7, 13, 23, 59)
    stored_utc_deadline = datetime(2026, 7, 13, 16, 0)

    planned = schedule_due_times(
        2,
        {},
        start_at=start,
        deadline_at=stored_utc_deadline,
        timezone_name="Asia/Shanghai",
        deadline_is_utc=True,
    )

    assert len(planned) == 2
    assert min(planned) >= start
    assert max(planned) < datetime(2026, 7, 14, 0, 0)


def test_channel_view_skips_account_capacity_time_after_deadline(monkeypatch) -> None:
    now_value = datetime(2026, 8, 10, 23, 50)
    local_deadline = datetime(2026, 8, 10, 23, 59, 59)
    stored_utc_deadline = datetime(2026, 8, 10, 15, 59, 59)
    task, message, context = _deadline_view_fixture(stored_utc_deadline)
    monkeypatch.setattr(channel_view, "_now", lambda: now_value)
    monkeypatch.setattr(channel_view, "schedule_times", lambda *_args, **_kwargs: [now_value])
    monkeypatch.setattr(
        channel_view,
        "reserve_task_schedule_times",
        lambda *_args, **_kwargs: [now_value],
    )
    monkeypatch.setattr(
        channel_view,
        "adjust_for_account_view_spacing",
        lambda _session, _task, _account_id, scheduled_at, **_kwargs: scheduled_at,
    )
    monkeypatch.setattr(
        channel_view,
        "adjust_for_account_hour_limit",
        lambda *_args, **_kwargs: local_deadline + timedelta(seconds=1),
    )
    ensure = Mock()
    monkeypatch.setattr(channel_view, "ensure_view_obligation", ensure)

    created = channel_view._create_view_actions(
        Mock(),
        task,
        actions=[(message, 31)],
        context=context,
    )

    assert created == 0
    ensure.assert_not_called()
    assert task.stats["channel_view_deadline_capacity_defer_count"] == 1
    assert "未创建跨日 Action" in task.last_error


def _deadline_view_fixture(deadline: datetime):
    task = SimpleNamespace(
        id="channel-view-deadline",
        timezone="Asia/Shanghai",
        pacing_config={},
        stats={},
        last_error="",
    )
    message = SimpleNamespace(id=901)
    context = channel_view.ViewCreationContext(
        channel=SimpleNamespace(),
        config={},
        execution_date="2026-08-10",
        ledger=SimpleNamespace(
            id="ledger-view",
            obligation_local_date=datetime(2026, 8, 10).date(),
            period_start_at=datetime(2026, 8, 10),
            deadline_at=deadline,
        ),
        targets_by_message={
            901: SimpleNamespace(
                daily_target_snapshot=10,
                total_target_snapshot=10,
            ),
        },
    )
    return task, message, context


def test_minimum_schedule_gap_uses_task_pacing_contract() -> None:
    assert minimum_schedule_gap_seconds(
        {"mode": "template", "template": "moderate_6h", "max_actions_per_hour": 1_000_000}
    ) == 180
    assert minimum_schedule_gap_seconds(
        {"mode": "fixed", "interval_seconds_min": 20, "interval_seconds_max": 60}
    ) == 20


def test_context_window_does_not_pull_reserved_ai_action_earlier(monkeypatch) -> None:
    now_value = datetime(2026, 8, 8, 18, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    task = Task(id="ai-context-reserved", tenant_id=1, name="AI 上下文排期", type="group_ai_chat", stats={})
    reserved_time = now_value + timedelta(minutes=10)

    turn_count, turn_times = group_ai_chat._limit_context_bound_turns(
        task,
        {"context_expire_after_messages": 1, "context_bound_schedule_window_seconds": 300},
        has_context=True,
        progress={},
        turn_count=1,
        planned_times=[reserved_time],
    )
    quality_items, quality_times = group_ai_chat._limit_context_bound_quality_schedule(
        task,
        {"context_expire_after_messages": 1, "context_bound_schedule_window_seconds": 300},
        has_context=True,
        progress={},
        quality_items=[{"content": "不应提前"}],
        planned_times=[reserved_time],
    )

    assert (turn_count, turn_times) == (0, [])
    assert quality_items == []
    assert quality_times == []


def test_operation_curve_preserves_template_gap_and_truncates_deadline() -> None:
    start = datetime(2026, 8, 8, 18, 0)
    deadline = start + timedelta(minutes=10)
    config = {
        "mode": "template",
        "template": "moderate_6h",
        "operation_profile": {"hourly_activity_curve": [10] * 24},
        "max_actions_per_hour": 1_000_000,
    }

    planned = schedule_times(
        1_000,
        config,
        start_at=start,
        deadline_at=deadline,
        preserve_minimum_spacing=True,
    )

    assert planned == [
        start,
        start + timedelta(minutes=3),
        start + timedelta(minutes=6),
        start + timedelta(minutes=9),
    ]
