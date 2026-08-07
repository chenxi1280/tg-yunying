from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import Task
from app.services.task_center.executors import group_ai_chat
from app.services.task_center.pacing import minimum_schedule_gap_seconds, schedule_times
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
