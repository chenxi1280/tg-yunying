from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.task_center.source_pacing_recovery import (
    late_admission_not_before,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 19, 22, 0)


def test_materially_overdue_actions_receive_stable_staggered_recovery_points() -> None:
    releases = [
        late_admission_not_before(
            action_id=f"overdue-action-{index}",
            release_at=NOW - timedelta(hours=8),
            now_at=NOW,
            gap_seconds=180,
            deadline_at=NOW + timedelta(hours=2),
        )
        for index in range(8)
    ]

    lower = NOW + timedelta(seconds=144)
    upper = NOW + timedelta(seconds=324)
    assert all(lower <= value <= upper for value in releases)
    assert len({value.replace(microsecond=0) for value in releases}) == len(releases)


def test_recovery_point_is_replay_stable() -> None:
    inputs = {
        "action_id": "stable-action",
        "release_at": NOW - timedelta(hours=1),
        "now_at": NOW,
        "gap_seconds": 180,
        "deadline_at": NOW + timedelta(hours=2),
    }

    assert late_admission_not_before(**inputs) == late_admission_not_before(**inputs)


def test_normal_dispatch_lag_does_not_rewrite_the_frozen_release() -> None:
    release_at = NOW - timedelta(seconds=5)

    result = late_admission_not_before(
        action_id="normal-lag",
        release_at=release_at,
        now_at=NOW,
        gap_seconds=180,
        deadline_at=NOW + timedelta(hours=1),
    )

    assert result == release_at


def test_recovery_never_crosses_the_source_deadline() -> None:
    deadline = NOW + timedelta(seconds=30)

    result = late_admission_not_before(
        action_id="deadline-action",
        release_at=NOW - timedelta(hours=1),
        now_at=NOW,
        gap_seconds=180,
        deadline_at=deadline,
    )

    assert result == deadline
