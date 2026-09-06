from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelDiscussionGroupBinding, ChannelMessage, ChannelMessageSourceRevision, OperationTarget
from app.services._common import _now
from app.timezone import as_beijing as _wall, as_beijing_aware
from .channel_comment_content_revision import reconcile_channel_comment_source_edit


def _upsert_channel_message(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    snapshot: Any,
    observed_at: Any,
    binding: ChannelDiscussionGroupBinding | None,
    thread_root_id: int,
) -> tuple[ChannelMessage | None, bool]:
    if int(snapshot.message_id or 0) <= 0:
        return None, False
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
    correction = _publication_correction(session, message, snapshot.published_at)
    revision, revision_created = _append_source_revision(
        session, source, message, snapshot=snapshot,
        published_at=published_at, observed_at=observed_at,
        binding=binding, thread_root_id=thread_root_id, timestamp_corrected=bool(correction),
    )
    message.message_url = snapshot.message_url or message.message_url or _message_url(channel, snapshot.message_id)
    message.content_preview = snapshot.content_preview or message.content_preview
    message.grouped_id = str(getattr(snapshot, "grouped_id", "") or "")
    message.source_metadata = _source_metadata(message, snapshot, correction)
    message.comment_available = bool(snapshot.comment_available)
    message.published_at = published_at or message.published_at
    if revision is not None:
        message.current_source_revision_id = revision.id
        if revision.source_operation == "edited":
            reconcile_channel_comment_source_edit(
                session, message, revision, at=_wall(observed_at),
            )
    return message, revision_created


def _publication_correction(session: Session, message: ChannelMessage, raw_time) -> dict:
    stored, normalized = message.published_at, _wall(raw_time)
    if stored is None or normalized is None:
        return {}
    if stored == normalized:
        previous = dict((message.source_metadata or {}).get("source_time_correction") or {})
        return previous if previous.get("previous_revision_id") == message.current_source_revision_id else {}
    if raw_time.tzinfo is None or stored != raw_time.replace(tzinfo=None):
        raise RuntimeError("source_published_at_conflict")
    previous = session.get(ChannelMessageSourceRevision, message.current_source_revision_id) if message.current_source_revision_id else None
    if previous is not None and _wall(previous.source_published_at) != stored:
        raise RuntimeError("source_published_at_conflict")
    return {"normalization_version": "beijing_instant_v1", "previous_value": stored.isoformat(),
            "corrected_value": as_beijing_aware(normalized).isoformat(),
            "previous_revision_id": message.current_source_revision_id or ""}


def _source_metadata(message, snapshot, correction):
    metadata = dict(getattr(snapshot, "source_metadata", {}) or {})
    provenance = correction or (message.source_metadata or {}).get("source_time_correction")
    return {**metadata, "source_time_correction": provenance} if provenance else metadata


def _append_source_revision(
    session: Session,
    source: Any,
    message: ChannelMessage,
    *,
    snapshot: Any,
    published_at: Any,
    observed_at: Any,
    binding: ChannelDiscussionGroupBinding | None,
    thread_root_id: int,
    timestamp_corrected: bool = False,
) -> tuple[ChannelMessageSourceRevision | None, bool]:
    if published_at is None:
        return None, False
    text = str(getattr(snapshot, "content_text", "") or snapshot.content_preview or "")
    edited_at = getattr(snapshot, "edited_at", None)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = _source_observation_hash(
        source.tenant_id,
        channel_target_id=message.channel_target_id,
        remote_message_id=int(snapshot.message_id),
        published_at=published_at,
        edited_at=edited_at,
        content_hash=content_hash,
        binding=binding,
        thread_root_id=thread_root_id,
    )
    existing_query = select(ChannelMessageSourceRevision).where(ChannelMessageSourceRevision.observation_identity_hash == identity)
    existing = session.scalar(existing_query)
    if existing is not None:
        return existing, False
    current = session.get(ChannelMessageSourceRevision, message.current_source_revision_id) if message.current_source_revision_id else None
    latest_query = select(func.max(ChannelMessageSourceRevision.source_revision)).where(ChannelMessageSourceRevision.channel_message_id == message.id)
    latest = session.scalar(latest_query) or 0
    revision = _new_source_revision(
        source, message,
        snapshot=snapshot,
        text=text,
        content_hash=content_hash,
        observation_identity=identity,
        revision_number=int(latest) + 1,
        published_at=published_at,
        observed_at=observed_at,
        edited_at=edited_at,
        binding=binding,
        source_operation=_revision_operation(current, content_hash, edited_at, timestamp_corrected=timestamp_corrected),
    )
    session.add(revision)
    session.flush()
    return revision, True


def _revision_operation(current, content_hash, edited_at, *, timestamp_corrected):
    operation = _source_operation(current, content_hash, edited_at)
    return "timestamp_corrected" if timestamp_corrected and operation == "observed" else operation


def _new_source_revision(
    source: Any,
    message: ChannelMessage,
    *,
    snapshot: Any,
    text: str,
    content_hash: str,
    observation_identity: str,
    revision_number: int,
    published_at: Any,
    observed_at: Any,
    edited_at: Any,
    binding: ChannelDiscussionGroupBinding | None,
    source_operation: str,
) -> ChannelMessageSourceRevision:
    return ChannelMessageSourceRevision(
        tenant_id=source.tenant_id,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        source_revision=revision_number,
        source_remote_message_id=int(snapshot.message_id),
        source_published_at=as_beijing_aware(published_at),
        source_published_at_fact_id=(
            f"telegram_message_date:{message.channel_target_id}:{int(snapshot.message_id)}"
        ),
        telegram_edit_date=as_beijing_aware(edited_at) if edited_at is not None else None,
        source_observed_at=as_beijing_aware(observed_at),
        source_type=str(getattr(snapshot, "source_type", "message_text") or "message_text"),
        source_text_snapshot=text,
        source_content_hash=content_hash,
        observation_identity_hash=observation_identity,
        source_length=len(text),
        captured_length=len(text),
        truncation_state=(
            "complete" if getattr(snapshot, "content_complete", True)
            else "transport_truncated"
        ),
        source_operation=source_operation,
        discussion_group_binding_id=binding.id if binding else None,
        discussion_group_binding_revision=binding.binding_revision if binding else None,
        discussion_group_identity_hash=binding.identity_hash if binding else "",
    )


def _source_observation_hash(
    tenant_id: int,
    *,
    channel_target_id: int,
    remote_message_id: int,
    published_at: Any,
    edited_at: Any,
    content_hash: str,
    binding: ChannelDiscussionGroupBinding | None,
    thread_root_id: int,
) -> str:
    parts = [
        str(tenant_id), str(channel_target_id), str(remote_message_id),
        _wall(published_at).isoformat(),
        _wall(edited_at).isoformat() if edited_at is not None else "no_edit",
        content_hash,
    ]
    if binding is not None:
        parts.extend((
            binding.id,
            str(binding.binding_revision),
            binding.identity_hash,
            str(thread_root_id or 0),
        ))
    identity = ":".join(parts)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _source_operation(
    current: ChannelMessageSourceRevision | None,
    content_hash: str,
    edited_at: Any,
) -> str:
    if current is None:
        return "observed"
    if current.source_content_hash != content_hash:
        return "edited"
    current_edit = _wall(current.telegram_edit_date) if current.telegram_edit_date else None
    observed_edit = _wall(edited_at) if edited_at else None
    return "edited" if current_edit != observed_edit else "observed"


def _message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    peer = str(channel.tg_peer_id or "")
    if peer.startswith("-100") and peer[4:].isdigit():
        return f"https://t.me/c/{peer[4:]}/{message_id}"
    return ""


def ensure_channel_message_source_revision(
    session: Session,
    message: ChannelMessage,
) -> ChannelMessageSourceRevision:
    if message.current_source_revision_id:
        existing = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        if existing is not None and existing.channel_message_id == message.id:
            return existing

    existing = session.scalar(
        select(ChannelMessageSourceRevision)
        .where(
            ChannelMessageSourceRevision.channel_message_id == message.id,
            ChannelMessageSourceRevision.tenant_id == message.tenant_id,
        )
        .order_by(ChannelMessageSourceRevision.source_revision.desc())
    )
    if existing is not None:
        message.current_source_revision_id = existing.id
        session.flush()
        return existing

    published_at = _wall(message.published_at or message.created_at or _now())
    text = str(message.content_preview or "")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = _source_observation_hash(
        message.tenant_id,
        channel_target_id=message.channel_target_id,
        remote_message_id=int(message.message_id),
        published_at=published_at,
        edited_at=None,
        content_hash=content_hash,
        binding=None,
        thread_root_id=0,
    )
    existing_by_hash = session.scalar(
        select(ChannelMessageSourceRevision).where(
            ChannelMessageSourceRevision.observation_identity_hash == identity
        )
    )
    if existing_by_hash is not None:
        message.current_source_revision_id = existing_by_hash.id
        session.flush()
        return existing_by_hash

    revision = ChannelMessageSourceRevision(
        tenant_id=message.tenant_id,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=int(message.message_id),
        source_published_at=as_beijing_aware(published_at),
        source_published_at_fact_id=(
            f"telegram_message_date:{message.channel_target_id}:{int(message.message_id)}"
        ),
        telegram_edit_date=None,
        source_observed_at=as_beijing_aware(message.created_at or published_at),
        source_type="message_text",
        source_text_snapshot=text,
        source_content_hash=content_hash,
        observation_identity_hash=identity,
        source_length=len(text),
        captured_length=len(text),
        truncation_state="complete",
        source_operation="observed",
        discussion_group_binding_id=None,
        discussion_group_binding_revision=None,
        discussion_group_identity_hash="",
    )
    session.add(revision)
    session.flush()
    message.current_source_revision_id = revision.id
    session.flush()
    return revision

