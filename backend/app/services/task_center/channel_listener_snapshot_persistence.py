from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import hashlib
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    ListenerChannelSnapshotItem,
    ListenerSourceState,
    OperationTarget,
)
from .channel_comment_content_revision import reconcile_channel_comment_source_edit
from .channel_comment_source_delete import settle_channel_comment_source_deleted
FRESHNESS_MULTIPLIER = 2


def persist_channel_snapshot(
    session: Session,
    source: Any,
    *,
    state_id: str,
    snapshots: list[Any],
    deletion_observations: list[Any] | None = None,
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
        message = _upsert_channel_message(
            session, source, channel, snapshot=snapshot, observed_at=now_value,
        )
        if message is None:
            continue
        session.flush()
        session.add(ListenerChannelSnapshotItem(
            listener_source_state_id=state.id,
            snapshot_revision=next_revision,
            channel_message_id=message.id,
        ))
    _settle_deleted_messages(
        session, source, channel,
        observations=deletion_observations or [], observed_at=now_value,
    )
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


def _settle_deleted_messages(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    observations: list[Any],
    observed_at: Any,
) -> None:
    for observation in observations:
        if not observation.deleted:
            continue
        message = session.scalar(select(ChannelMessage).where(
            ChannelMessage.tenant_id == source.tenant_id,
            ChannelMessage.channel_target_id == channel.id,
            ChannelMessage.message_id == int(observation.message_id),
        ))
        if message is None:
            continue
        evidence = ":".join((
            str(source.tenant_id), str(channel.id), str(observation.message_id),
            str(observation.evidence_kind),
        ))
        settle_channel_comment_source_deleted(
            session, message,
            occurred_at=_wall(observed_at),
            evidence_hash=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        )


def _upsert_channel_message(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    snapshot: Any,
    observed_at: Any,
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
        session.flush()
    if message.published_at and published_at and message.published_at != published_at:
        raise RuntimeError("source_published_at_conflict")
    revision = _append_source_revision(
        session, source, message, snapshot=snapshot,
        published_at=published_at, observed_at=observed_at,
    )
    message.message_url = snapshot.message_url or message.message_url or _message_url(channel, snapshot.message_id)
    message.content_preview = snapshot.content_preview or message.content_preview
    message.comment_available = bool(snapshot.comment_available)
    message.published_at = published_at or message.published_at
    if revision is not None:
        message.current_source_revision_id = revision.id
        if revision.source_operation == "edited":
            reconcile_channel_comment_source_edit(
                session, message, revision, at=_wall(observed_at),
            )
    return message


def _append_source_revision(
    session: Session,
    source: Any,
    message: ChannelMessage,
    *,
    snapshot: Any,
    published_at: Any,
    observed_at: Any,
) -> ChannelMessageSourceRevision | None:
    if published_at is None:
        return None
    text = str(snapshot.content_preview or "")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = _source_observation_hash(
        source.tenant_id, message.channel_target_id,
        int(snapshot.message_id), published_at, content_hash,
    )
    existing = session.scalar(select(ChannelMessageSourceRevision).where(
        ChannelMessageSourceRevision.observation_identity_hash == identity,
    ))
    if existing is not None:
        return existing
    latest = session.scalar(select(func.max(ChannelMessageSourceRevision.source_revision)).where(
        ChannelMessageSourceRevision.channel_message_id == message.id,
    )) or 0
    revision = ChannelMessageSourceRevision(
        tenant_id=source.tenant_id,
        channel_message_id=message.id,
        source_revision=int(latest) + 1,
        source_remote_message_id=int(snapshot.message_id),
        source_published_at=published_at,
        source_observed_at=_wall(observed_at),
        source_text_snapshot=text,
        source_content_hash=content_hash,
        observation_identity_hash=identity,
        source_operation="observed" if not latest else "edited",
    )
    session.add(revision)
    session.flush()
    return revision


def _source_observation_hash(
    tenant_id: int,
    channel_target_id: int,
    remote_message_id: int,
    published_at: Any,
    content_hash: str,
) -> str:
    identity = ":".join((
        str(tenant_id), str(channel_target_id), str(remote_message_id),
        _wall(published_at).isoformat(), content_hash,
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    peer = str(channel.tg_peer_id or "")
    if peer.startswith("-100") and peer[4:].isdigit():
        return f"https://t.me/c/{peer[4:]}/{message_id}"
    return ""


def _wall(value):
    return value.replace(tzinfo=None) if value.tzinfo else value
