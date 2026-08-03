from types import SimpleNamespace

import pytest

from app.services.task_center.executors import group_ai_chat
from app.services.task_center.ai_reply_allocation import cumulative_reply_requirement


pytestmark = pytest.mark.no_postgres


def test_daily_reply_minimum_uses_configured_round_denominator() -> None:
    requirement = cumulative_reply_requirement

    assert requirement(
        prior_total=0,
        prior_reply=0,
        batch_total=4,
        round_total=60,
        round_reply=12,
    ) == 0
    assert requirement(
        prior_total=4,
        prior_reply=0,
        batch_total=1,
        round_total=60,
        round_reply=12,
    ) == 1
    assert requirement(
        prior_total=180,
        prior_reply=180,
        batch_total=4,
        round_total=60,
        round_reply=12,
    ) == 0


def test_daily_coverage_debt_falls_back_to_direct_slots_when_replies_are_short(
    monkeypatch,
) -> None:
    task = SimpleNamespace(stats={}, last_error="")
    monkeypatch.setattr(
        group_ai_chat,
        "_group_reply_target_pool",
        lambda *_args, **_kwargs: [],
    )

    targets, coverage_shortfall = group_ai_chat._reply_targets_for_plan(
        SimpleNamespace(),
        task,
        SimpleNamespace(),
        [],
        3,
        {"reply_min_per_round": 2},
        {},
        daily_coverage_debt=True,
    )

    assert targets == []
    assert coverage_shortfall is True
    assert task.stats["reply_target_shortfall_count"] == 1
    assert task.stats["coverage_reply_shortfall_cycle_count"] == 1
    assert task.last_error == ""


def test_ordinary_cycle_still_waits_when_required_replies_are_short(
    monkeypatch,
) -> None:
    task = SimpleNamespace(stats={}, last_error="")
    monkeypatch.setattr(
        group_ai_chat,
        "_group_reply_target_pool",
        lambda *_args, **_kwargs: [],
    )

    targets, coverage_shortfall = group_ai_chat._reply_targets_for_plan(
        SimpleNamespace(),
        task,
        SimpleNamespace(),
        [],
        3,
        {"reply_min_per_round": 2},
        {},
        daily_coverage_debt=False,
    )

    assert targets is None
    assert coverage_shortfall is False
    assert task.stats["reply_target_shortfall_count"] == 1
    assert "可引用消息不足" in task.last_error
