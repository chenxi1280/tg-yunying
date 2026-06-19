from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.schemas.task_center import OperationProfile
from app.services.task_center.pacing import ai_next_run_after
from app.services.task_center.executors import group_ai_chat


def _curve(hour: int, rounds: int) -> list[int]:
    values = [0 for _ in range(24)]
    values[hour] = rounds
    return values


def test_operation_profile_treats_curve_values_as_hourly_rounds() -> None:
    profile = OperationProfile(hourly_activity_curve=_curve(20, 60))

    assert profile.hourly_activity_curve[20] == 60


def test_operation_profile_rejects_hourly_rounds_above_product_limit() -> None:
    with pytest.raises(ValueError, match="每小时轮数"):
        OperationProfile(hourly_activity_curve=_curve(20, 61))


def test_auto_messages_per_round_uses_current_hour_rounds(monkeypatch) -> None:
    monkeypatch.setattr(group_ai_chat, "_now", lambda: datetime(2026, 6, 3, 20, 0, 0))
    pacing_config = {
        "max_actions_per_hour": 120,
        "operation_profile": {"hourly_activity_curve": _curve(20, 6)},
    }

    assert group_ai_chat._auto_messages_per_round({}, "高峰期", True, pacing_config) == 20


def test_participant_count_does_not_apply_curve_ramp_ratio(monkeypatch) -> None:
    accounts = [object() for _ in range(100)]
    monkeypatch.setattr(group_ai_chat.random, "uniform", lambda _lo, _hi: 1.0)

    assert group_ai_chat._desired_participant_count(accounts, {"participation_rate": 0.8}, "低频期", 0.1) == 80


def test_daily_coverage_priority_is_not_rotated_out(monkeypatch) -> None:
    accounts = [SimpleNamespace(id=account_id) for account_id in [3, 4, 5, 1, 2]]
    monkeypatch.setattr(group_ai_chat.random, "uniform", lambda _lo, _hi: 1.0)

    selected, turn_count = group_ai_chat._select_cycle_accounts(
        accounts,
        {"messages_per_round_mode": "manual", "messages_per_round": 2, "participation_rate": 0.4},
        "正常期",
        1.0,
        has_context=True,
        cycle_index=4,
        daily_coverage_uncovered_count=3,
    )

    assert [account.id for account in selected[:3]] == [3, 4, 5]
    assert turn_count == 3


def test_daily_coverage_minimum_turns_can_exceed_participation_rate(monkeypatch) -> None:
    accounts = [SimpleNamespace(id=account_id) for account_id in range(1, 7)]
    monkeypatch.setattr(group_ai_chat.random, "uniform", lambda _lo, _hi: 1.0)

    selected, turn_count = group_ai_chat._select_cycle_accounts(
        accounts,
        {"messages_per_round_mode": "manual", "messages_per_round": 2, "participation_rate": 0.2},
        "正常期",
        1.0,
        has_context=True,
        cycle_index=1,
        daily_coverage_uncovered_count=5,
    )

    assert [account.id for account in selected[:5]] == [1, 2, 3, 4, 5]
    assert turn_count == 5


def test_ai_next_run_after_uses_hourly_round_interval() -> None:
    pacing_config = {"operation_profile": {"hourly_activity_curve": _curve(20, 6)}}

    assert ai_next_run_after(pacing_config, datetime(2026, 6, 3, 20, 0, 0)) == datetime(2026, 6, 3, 20, 10, 0)


def test_ai_next_run_after_skips_sleep_hours() -> None:
    pacing_config = {"operation_profile": {"hourly_activity_curve": _curve(20, 6)}}

    assert ai_next_run_after(pacing_config, datetime(2026, 6, 3, 19, 30, 0)) == datetime(2026, 6, 3, 20, 0, 0)
