from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.task_center.dispatch_release_wave import (
    start_or_join_dispatch_rebuild_wave,
)
from app.timezone import BEIJING_TZ


pytestmark = pytest.mark.no_postgres


def test_expired_aware_window_accepts_naive_business_clock() -> None:
    now_value = datetime(2026, 7, 30, 2, 30)
    window = SimpleNamespace(
        bucket_end=(now_value - timedelta(minutes=1)).replace(tzinfo=BEIJING_TZ),
        unclaimed_allocated_count=1,
        effective_unclaimed_count=1,
        allocation_state="ready",
        allocation_epoch=1,
        rebuild_input_version=0,
        pending_rebuild_release_count=0,
        version=1,
    )
    session = SimpleNamespace(get=lambda *_args: window)

    result = start_or_join_dispatch_rebuild_wave(
        session,
        window_id="expired-window",
        released_count=1,
        now_value=now_value,
    )

    assert result is None
    assert window.unclaimed_allocated_count == 0
    assert window.allocation_state == "ready"
