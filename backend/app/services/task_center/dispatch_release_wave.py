from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import DispatchClaimWindow

from .datetime_compat import is_after_or_equal


def start_or_join_dispatch_rebuild_wave(
    session: Session,
    *,
    window_id: str,
    released_count: int,
    now_value: datetime,
    decrement_unclaimed: bool = True,
) -> int | None:
    if released_count <= 0:
        return 0
    window = session.get(DispatchClaimWindow, window_id)
    if window is None:
        raise RuntimeError("dispatch_release_window_missing")
    if decrement_unclaimed:
        window.unclaimed_allocated_count -= released_count
        if window.unclaimed_allocated_count < 0:
            raise RuntimeError("dispatch_release_window_unclaimed_negative")
    if is_after_or_equal(now_value, window.bucket_end):
        return None
    if window.allocation_state == "ready":
        window.allocation_epoch += 1
        window.allocation_state = "rebuild_required"
    window.rebuild_input_version += 1
    window.pending_rebuild_release_count += released_count
    window.version += 1
    return int(window.rebuild_input_version)


__all__ = ["start_or_join_dispatch_rebuild_wave"]
