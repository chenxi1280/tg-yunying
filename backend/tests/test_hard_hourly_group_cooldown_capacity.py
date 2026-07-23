from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.task_center.coverage_capacity import (
    HARD_HOURLY_GROUP_COOLDOWN_BLOCKER_CODE,
    hard_hourly_group_cooldown_proof,
    hard_hourly_required_hourly_messages,
)

pytestmark = pytest.mark.no_postgres


def test_hard_hourly_required_caps_full_backfill_debt_to_planning_rate() -> None:
    assert hard_hourly_required_hourly_messages(hourly_target=120, backfill_planning_deficit=0) == 120
    assert hard_hourly_required_hourly_messages(hourly_target=120, backfill_planning_deficit=50) == 170
    assert hard_hourly_required_hourly_messages(hourly_target=120, backfill_planning_deficit=2400) == 240
    # Legacy callers may still pass the full multi-hour deficit; cap to planning rate.
    assert (
        hard_hourly_required_hourly_messages(
            hourly_target=120,
            backfill_planning_deficit=2400,
            required_hourly_messages=2722,
        )
        == 240
    )


def test_group_cooldown_proof_allows_15s_for_full_planning_rate_with_backfill() -> None:
    group = SimpleNamespace(group_cooldown_seconds=15)
    proof = hard_hourly_group_cooldown_proof(
        group=group,
        hourly_target=120,
        backfill_planning_deficit=2000,
        required_hourly_messages=2500,
    )
    assert proof["required_hourly_messages"] == 240
    assert proof["group_cooldown_hourly_capacity"] == 240
    assert proof["sufficient"] is True


def test_group_cooldown_proof_allows_20s_for_goal_without_backfill() -> None:
    group = SimpleNamespace(group_cooldown_seconds=20)
    proof = hard_hourly_group_cooldown_proof(group=group, hourly_target=120, backfill_planning_deficit=0)
    assert proof["group_cooldown_hourly_capacity"] == 180
    assert proof["required_hourly_messages"] == 120
    assert proof["sufficient"] is True
    assert proof["blocker_code"] == ""


def test_group_cooldown_proof_blocks_60s_for_120_goal() -> None:
    group = SimpleNamespace(group_cooldown_seconds=60)
    proof = hard_hourly_group_cooldown_proof(group=group, hourly_target=120, backfill_planning_deficit=0)
    assert proof["group_cooldown_hourly_capacity"] == 60
    assert proof["sufficient"] is False
    assert proof["blocker_code"] == HARD_HOURLY_GROUP_COOLDOWN_BLOCKER_CODE
