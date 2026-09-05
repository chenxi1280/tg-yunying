from datetime import datetime

from sqlalchemy import select

from app.models import ChannelMessage
from app.timezone import as_beijing


def _normalize_snapshot_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return as_beijing(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return as_beijing(parsed)
    except ValueError:
        return None


def _record_channel_time_correction(session, message, snapshot) -> None:
    from .task_center.channel_source_message_persistence import _publication_correction, _source_metadata

    correction = _publication_correction(session, message, snapshot.published_at)
    if correction:
        message.source_metadata = _source_metadata(message, snapshot, correction)


def persist_channel_message_snapshot(session, target, snapshot, *, message_url) -> int:
    message_id = int(snapshot.message_id or 0)
    if message_id <= 0:
        return 0
    existing = session.scalar(
        select(ChannelMessage).where(
            ChannelMessage.tenant_id == target.tenant_id,
            ChannelMessage.channel_target_id == target.id,
            ChannelMessage.message_id == message_id,
        )
    )
    published_at = _normalize_snapshot_datetime(snapshot.published_at)
    if existing:
        _record_channel_time_correction(session, existing, snapshot)
        existing.content_preview = snapshot.content_preview or existing.content_preview
        existing.message_url = snapshot.message_url or existing.message_url or message_url(target, message_id)
        existing.comment_available = bool(snapshot.comment_available)
        existing.published_at = published_at or existing.published_at
        return 0
    session.add(
        ChannelMessage(
            tenant_id=target.tenant_id,
            channel_target_id=target.id,
            message_id=message_id,
            message_url=snapshot.message_url or message_url(target, message_id),
            content_preview=snapshot.content_preview,
            comment_available=bool(snapshot.comment_available),
            published_at=published_at,
        )
    )
    return 1
