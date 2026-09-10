from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Task
from app.models.group_clone import CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateState, TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now

SOURCE_STREAM_STATES = ("catching_up", "live", "gap")
CHANNEL_TOO_LONG = "group_clone_channel_difference_too_long"
RECOVERABLE_ERRORS = frozenset({
    "group_clone_source_pts_gap", "group_clone_channel_difference_incomplete",
})
COLLECTOR_TASK_STATES = ("pending", "running", "paused", "failed")


def apply_channel_batch(session, state, batch, *, peer_id) -> None:
    if not peer_id:
        raise RuntimeError("telegram_channel_difference_peer_missing")
    session.flush()
    current = dict(state.difference_cursor or {})
    channels = dict(current.get("channels") or {})
    channels[peer_id] = {
        **dict(batch.cursor or {}), "status": batch.status, "final": bool(batch.final),
    }
    state.difference_cursor = {**current, "channels": channels}
    state.last_applied_at = _now()
    for task, stream in _locked_channel_streams(session, state.id, peer_id):
        if batch.status == "too_long" or task.last_error == CHANNEL_TOO_LONG:
            observed_pts = int(batch.cursor.get("pts") or 0) if batch.status == "too_long" else 0
            _lose_continuity(task, stream, observed_pts=observed_pts)
        elif not batch.final:
            _block_stream(task, stream)
        else:
            _recover_stream(task, stream)
    session.flush()
    clear_channel_error_from_tasks(session, state.id, peer_id)


def _locked_channel_streams(session, state_id, peer_id):
    # Lock current Task intent before its stream, matching lifecycle operations.
    tasks = session.scalars(
        select(Task).join(CloneSourceStreamState, CloneSourceStreamState.task_id == Task.id)
        .where(
            CloneSourceStreamState.authorization_update_state_id == state_id,
            CloneSourceStreamState.source_peer_id == peer_id,
            CloneSourceStreamState.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
            Task.type == "group_clone", Task.status.in_(COLLECTOR_TASK_STATES),
            Task.deleted_at.is_(None), Task.retired_at.is_(None),
        ).order_by(Task.id).with_for_update(of=Task, key_share=True)
        .execution_options(populate_existing=True)
    ).all()
    for task in tasks:
        if task.status == "failed" and task.last_error not in RECOVERABLE_ERRORS | {CHANNEL_TOO_LONG}:
            continue
        stream = session.scalar(select(CloneSourceStreamState).where(
            CloneSourceStreamState.task_id == task.id,
            CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
            CloneSourceStreamState.authorization_update_state_id == state_id,
            CloneSourceStreamState.source_peer_id == peer_id,
            CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
        ).with_for_update().execution_options(populate_existing=True))
        if stream is not None:
            yield task, stream


def _lose_continuity(task, stream, *, observed_pts) -> None:
    stream.state = "blocked"
    stream.difference_cursor = {
        **dict(stream.difference_cursor or {}),
        "continuity_lost": {
            "reason": CHANNEL_TOO_LONG, "last_consumed_pts": stream.channel_pts,
            "observed_pts": observed_pts, "observed_at": _now().isoformat(),
        },
    }
    _mark_runtime_blocked(task, CHANNEL_TOO_LONG)


def _block_stream(task, stream) -> None:
    stream.state = "gap"
    _mark_runtime_blocked(task, "group_clone_channel_difference_incomplete")


def _mark_runtime_blocked(task, reason) -> None:
    task.last_error = reason
    if task.status == "paused":
        return
    task.status = "failed"
    task.next_run_at = None
    task.stats = {**dict(task.stats or {}), "clone_start_state": "runtime_blocked"}


def _recover_stream(task, stream) -> None:
    if stream.state != "gap":
        return
    stream.state = "catching_up"
    stream.version = int(stream.version or 1) + 1
    if task.last_error in RECOVERABLE_ERRORS:
        task.last_error = ""
    if task.status == "paused":
        return
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "runtime_recovering"}


def channels_without_cursor(session_factory, state_id: str) -> list[str]:
    with session_factory() as session:
        state = session.get(TelegramAuthorizationUpdateState, state_id)
        channels = dict((state.difference_cursor or {}).get("channels") or {})
        stream_pts = dict(session.execute(
            select(
                CloneSourceStreamState.source_peer_id,
                func.max(CloneSourceStreamState.channel_pts),
            )
            .where(
                CloneSourceStreamState.authorization_update_state_id == state_id,
                CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
            )
            .group_by(CloneSourceStreamState.source_peer_id)
        ).all())
        peer_ids = list(session.scalars(
            select(TelegramAuthorizationUpdateSubscription.source_peer_id)
            .where(
                TelegramAuthorizationUpdateSubscription.authorization_update_state_id
                == state_id,
                TelegramAuthorizationUpdateSubscription.source_peer_type == "channel",
                TelegramAuthorizationUpdateSubscription.state.in_(("initializing", "active")),
            )
            .distinct()
        ))
        return [
            str(peer_id)
            for peer_id in peer_ids
            if max(
                int((channels.get(str(peer_id)) or {}).get("pts") or 0),
                int(stream_pts.get(str(peer_id)) or 0),
            ) <= 0
        ]


def project_channel_error_to_tasks(
    session: Session,
    state_id: str,
    peer_id: str,
    *,
    detail: str,
) -> None:
    subscriptions = list(session.scalars(
        select(TelegramAuthorizationUpdateSubscription).where(
            TelegramAuthorizationUpdateSubscription.authorization_update_state_id
            == state_id,
            TelegramAuthorizationUpdateSubscription.source_peer_type == "channel",
            TelegramAuthorizationUpdateSubscription.source_peer_id == str(peer_id),
            TelegramAuthorizationUpdateSubscription.state == "active",
        )
    ))
    for subscription in subscriptions:
        task = _changed_channel_error_task(session, subscription, peer_id=peer_id, detail=detail)
        if task is None:
            continue
        errors = dict((task.stats or {}).get("telegram_update_channel_errors") or {})
        errors[str(peer_id)] = detail
        task.stats = {
            **dict(task.stats or {}),
            "telegram_update_channel_errors": errors,
        }


def clear_channel_error_from_tasks(
    session: Session,
    state_id: str,
    peer_id: str,
) -> None:
    subscriptions = list(session.scalars(
        select(TelegramAuthorizationUpdateSubscription).where(
            TelegramAuthorizationUpdateSubscription.authorization_update_state_id
            == state_id,
            TelegramAuthorizationUpdateSubscription.source_peer_type == "channel",
            TelegramAuthorizationUpdateSubscription.source_peer_id == str(peer_id),
            TelegramAuthorizationUpdateSubscription.state == "active",
        )
    ))
    for subscription in subscriptions:
        task = _changed_channel_error_task(session, subscription, peer_id=peer_id)
        if task is None:
            continue
        stats = dict(task.stats or {})
        errors = dict(stats.get("telegram_update_channel_errors") or {})
        errors.pop(str(peer_id), None)
        if errors:
            stats["telegram_update_channel_errors"] = errors
        else:
            stats.pop("telegram_update_channel_errors", None)
        task.stats = stats


def _changed_channel_error_task(session, subscription, *, peer_id, detail=None):
    session.flush()
    task = session.get(Task, subscription.task_id, populate_existing=True)
    if task is None:
        return None
    errors = dict((task.stats or {}).get("telegram_update_channel_errors") or {})
    if detail is None and str(peer_id) not in errors:
        return None
    if detail is not None and errors.get(str(peer_id)) == detail:
        return None
    return _current_subscription_task(session, subscription)


def _current_subscription_task(session, subscription):
    task = session.get(Task, subscription.task_id, with_for_update={"key_share": True}, populate_existing=True)
    if task is None or task.task_lifecycle_epoch != subscription.task_epoch:
        return None
    if task.deleted_at is not None or task.retired_at is not None:
        return None
    return task


def channel_cursors(session_factory, state_id: str) -> list[tuple[str, int]]:
    with session_factory() as session:
        state = session.get(TelegramAuthorizationUpdateState, state_id)
        channel_state = dict((state.difference_cursor or {}).get("channels") or {})
        stream_rows = session.execute(
            select(
                CloneSourceStreamState.source_peer_id,
                func.min(CloneSourceStreamState.channel_pts),
            )
            .join(Task, Task.id == CloneSourceStreamState.task_id)
            .where(
                CloneSourceStreamState.authorization_update_state_id == state_id,
                CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
                CloneSourceStreamState.channel_pts > 0,
                Task.status.in_(("pending", "running", "failed")),
                Task.task_lifecycle_epoch == CloneSourceStreamState.task_lifecycle_epoch,
            )
            .group_by(CloneSourceStreamState.source_peer_id)
        ).all()
        subscription_peer_ids = list(session.scalars(
            select(TelegramAuthorizationUpdateSubscription.source_peer_id)
            .where(
                TelegramAuthorizationUpdateSubscription.authorization_update_state_id
                == state_id,
                TelegramAuthorizationUpdateSubscription.source_peer_type == "channel",
                TelegramAuthorizationUpdateSubscription.state == "active",
            )
            .distinct()
        ))
        cursors = {
            str(peer_id): int((channel_state.get(str(peer_id)) or {}).get("pts") or 0)
            for peer_id in subscription_peer_ids
        }
        for peer_id, pts in stream_rows:
            key = str(peer_id)
            cursors[key] = max(cursors.get(key, 0), int(pts or 0))
        return [
            (peer_id, max(pts, int((channel_state.get(peer_id) or {}).get("pts") or 0)))
            for peer_id, pts in cursors.items()
            if max(pts, int((channel_state.get(peer_id) or {}).get("pts") or 0)) > 0
        ]
