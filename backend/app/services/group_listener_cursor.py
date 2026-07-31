"""Persist ordinary group listener cursor continuity facts."""

from __future__ import annotations

from app.models import TgGroup


def listener_after_message_id(group: TgGroup) -> int | None:
    from .task_center.group_bot_observation import numeric_cursor

    return numeric_cursor(getattr(group, "listener_remote_cursor", ""))


def update_listener_cursor(
    group: TgGroup,
    snapshots: list[object],
    *,
    after_message_id: int | None = None,
    fetch_limit: int | None = None,
) -> None:
    from .task_center.group_bot_observation import numeric_cursor, snapshot_cursor_bounds

    lower, upper = snapshot_cursor_bounds(snapshots)
    current = numeric_cursor(getattr(group, "listener_remote_cursor", ""))
    if after_message_id is not None:
        _update_anchored_cursor(
            group,
            snapshots,
            after_message_id=after_message_id,
            fetch_limit=fetch_limit,
            lower=lower,
            upper=upper,
            current=current,
        )
        return
    if lower is None or upper is None:
        group.listener_cursor_status = "unproven"
        return
    if current is None:
        group.listener_remote_cursor = str(upper)
        group.listener_cursor_status = "contiguous"
        return
    if upper == current or (upper > current and lower <= current + 1):
        group.listener_remote_cursor = str(max(current, upper))
        group.listener_cursor_status = "contiguous"
        return
    if upper < current:
        group.listener_cursor_status = "unproven"
        return
    group.listener_cursor_status = "gap"


def _update_anchored_cursor(
    group: TgGroup,
    snapshots: list[object],
    *,
    after_message_id: int,
    fetch_limit: int | None,
    lower: int | None,
    upper: int | None,
    current: int | None,
) -> None:
    from .task_center.group_bot_observation import numeric_cursor

    if current != after_message_id:
        group.listener_cursor_status = "unproven"
        return
    if not snapshots:
        group.listener_cursor_status = "contiguous"
        return
    if (
        lower is None
        or upper is None
        or lower <= after_message_id
        or any(numeric_cursor(getattr(item, "remote_message_id", "")) is None for item in snapshots)
    ):
        group.listener_cursor_status = "unproven"
        return
    group.listener_remote_cursor = str(upper)
    limit = max(1, int(fetch_limit or 1))
    group.listener_cursor_status = "unproven" if len(snapshots) >= limit else "contiguous"


__all__ = ["listener_after_message_id", "update_listener_cursor"]
