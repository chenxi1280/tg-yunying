from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.task_center.pacing import schedule_times


pytestmark = pytest.mark.no_postgres


def test_quiet_hours_shift_preserves_original_action_spacing() -> None:
    start = datetime(2026, 8, 7, 23, 45)
    config = {
        "mode": "fixed",
        "interval_seconds_min": 300,
        "interval_seconds_max": 300,
        "quiet_hours": {"start": "23:00", "end": "18:00"},
    }

    times = schedule_times(3, config, start_at=start)

    assert times == [
        datetime(2026, 8, 8, 18, 0),
        datetime(2026, 8, 8, 18, 5),
        datetime(2026, 8, 8, 18, 10),
    ]
    assert all(current - previous == timedelta(minutes=5) for previous, current in zip(times, times[1:]))
