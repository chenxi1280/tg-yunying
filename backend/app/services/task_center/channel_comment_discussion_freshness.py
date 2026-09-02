from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelDiscussionGroupBinding,
    ChannelDiscussionGroupProbeEvent,
    ChannelDiscussionThreadBinding,
    ChannelDiscussionThreadProbeEvent,
)


def group_binding_fresh(
    session: Session,
    binding: ChannelDiscussionGroupBinding,
    now_value: datetime,
) -> bool:
    event = session.scalar(select(ChannelDiscussionGroupProbeEvent).where(
        ChannelDiscussionGroupProbeEvent.tenant_id == binding.tenant_id,
        ChannelDiscussionGroupProbeEvent.channel_target_id == binding.channel_target_id,
    ).order_by(ChannelDiscussionGroupProbeEvent.observed_at.desc()))
    return bool(
        event and event.probe_status == "success"
        and event.observed_linked_chat_id == binding.discussion_peer_id
        and event.fresh_until_at and _wall(event.fresh_until_at) >= _wall(now_value)
    )


def thread_binding_fresh(
    session: Session,
    binding: ChannelDiscussionThreadBinding,
    now_value: datetime,
) -> bool:
    event = session.scalar(select(ChannelDiscussionThreadProbeEvent).where(
        ChannelDiscussionThreadProbeEvent.source_revision_id == binding.source_revision_id,
        ChannelDiscussionThreadProbeEvent.group_binding_id == binding.group_binding_id,
    ).order_by(ChannelDiscussionThreadProbeEvent.observed_at.desc()))
    return bool(
        event and event.probe_status == "success"
        and event.observed_thread_root_message_id == binding.thread_root_message_id
        and event.fresh_until_at and _wall(event.fresh_until_at) >= _wall(now_value)
    )


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["group_binding_fresh", "thread_binding_fresh"]
