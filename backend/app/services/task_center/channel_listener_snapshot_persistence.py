from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, ListenerChannelSnapshotItem, ListenerSourceState, OperationTarget
FRESHNESS_MULTIPLIER = 2


def persist_channel_snapshot(
    session: Session,
    source: Any,
    *,
    state_id: str,
    snapshots: list[Any],
    reaction_capability: Any,
    now_value: Any,
    wake_subscribers: Callable[..., None],
) -> None:
    channel = session.get(OperationTarget, source.channel_target_id)
    state = session.get(ListenerSourceState, state_id)
    if channel is None or state is None:
        raise RuntimeError("channel_listener_state_lost")
    if reaction_capability is not None:
        channel.reaction_capability_mode = str(reaction_capability.mode)
        channel.available_reactions = list(reaction_capability.available_reactions)
    next_revision = int(state.snapshot_revision or 0) + 1
    session.execute(delete(ListenerChannelSnapshotItem).where(
        ListenerChannelSnapshotItem.listener_source_state_id == state.id
    ))
    for snapshot in snapshots:
        message = _upsert_channel_message(session, source, channel, snapshot=snapshot)
        if message is None:
            continue
        session.flush()
        session.add(ListenerChannelSnapshotItem(
            listener_source_state_id=state.id,
            snapshot_revision=next_revision,
            channel_message_id=message.id,
        ))
    state.snapshot_revision = next_revision
    state.snapshot_status = "ready"
    state.observed_at = now_value
    state.fresh_until_at = now_value + timedelta(
        seconds=source.collect_window_seconds * FRESHNESS_MULTIPLIER
    )
    state.next_probe_at = now_value + timedelta(seconds=source.collect_window_seconds)
    state.last_error = ""
    state.last_error_code = ""
    state.lease_owner = ""
    state.lease_expires_at = None
    wake_subscribers(session, source, state, reason="channel_source_snapshot_ready")


def _upsert_channel_message(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    snapshot: Any,
) -> ChannelMessage | None:
    if int(snapshot.message_id or 0) <= 0:
        return None
    message = session.scalar(select(ChannelMessage).where(
        ChannelMessage.tenant_id == source.tenant_id,
        ChannelMessage.channel_target_id == channel.id,
        ChannelMessage.message_id == int(snapshot.message_id),
    ))
    published_at = _wall(snapshot.published_at) if snapshot.published_at else None
    if message is None:
        message = ChannelMessage(
            tenant_id=source.tenant_id,
            channel_target_id=channel.id,
            message_id=int(snapshot.message_id),
        )
        session.add(message)
    message.message_url = snapshot.message_url or message.message_url or _message_url(channel, snapshot.message_id)
    message.content_preview = snapshot.content_preview or message.content_preview
    message.comment_available = bool(snapshot.comment_available)
    message.published_at = published_at or message.published_at
    return message


def _message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    peer = str(channel.tg_peer_id or "")
    if peer.startswith("-100") and peer[4:].isdigit():
        return f"https://t.me/c/{peer[4:]}/{message_id}"
    return ""


def _wall(value):
    return value.replace(tzinfo=None) if value.tzinfo else value
