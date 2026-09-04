from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import hashlib
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import (
    ChannelDiscussionGroupBinding,
    ChannelMessage,
    ChannelMessageSourceRevision,
    ListenerChannelSnapshotItem,
    ListenerSourceState,
    OperationTarget,
)
from .channel_comment_discussion_contracts import (
    AUTHORITATIVE_GROUP_STAGE,
    AUTHORITATIVE_THREAD_STAGE,
    GroupProbeObservation,
    ThreadProbeObservation,
    record_group_probe,
    record_thread_probe,
)
from .channel_comment_content_revision import reconcile_channel_comment_source_edit
from .channel_comment_source_delete import settle_channel_comment_source_deleted
from .channel_source_pagination import advance_source_page
from .channel_listener_claim import locked_source_state
FRESHNESS_MULTIPLIER = 2


def persist_channel_snapshot(
    session: Session,
    source: Any,
    *,
    state_id: str,
    snapshots: list[Any],
    deletion_observations: list[Any] | None = None,
    reaction_capability: Any,
    discussion_snapshot: Any | None = None,
    discussion_probe_error: str = "",
    now_value: Any,
    wake_subscribers: Callable[..., None],
) -> None:
    channel = session.get(OperationTarget, source.channel_target_id)
    state = locked_source_state(session, source, state_id)
    if channel is None or state is None:
        raise RuntimeError("channel_listener_state_lost")
    if reaction_capability is not None:
        channel.reaction_capability_mode = str(reaction_capability.mode)
        channel.available_reactions = list(reaction_capability.available_reactions)
    probe_request_id = f"{state.id}:{int(state.snapshot_revision or 0) + 1}:discussion"
    binding = _persist_discussion_binding(
        session, source, channel,
        probe_request_id=probe_request_id,
        snapshot=discussion_snapshot,
        error_code=discussion_probe_error,
        observed_at=now_value,
    )
    next_revision = int(state.snapshot_revision or 0) + 1
    _persist_snapshot_messages(
        session, source, channel,
        state=state,
        snapshots=snapshots,
        snapshot_revision=next_revision,
        binding=binding,
        discussion_snapshot=discussion_snapshot,
        probe_request_id=probe_request_id,
        observed_at=now_value,
    )
    _settle_deleted_messages(
        session, source, channel,
        observations=deletion_observations or [], observed_at=now_value,
    )
    progress = advance_source_page(session, source, state=state, snapshots=snapshots, observed_at=now_value)
    _mark_snapshot_ready(state, source, next_revision=next_revision, now_value=now_value)
    if not progress.complete:
        state.snapshot_status = "catching_up"
        state.fresh_until_at = None
    else:
        state.observed_at = progress.observed_at
        if getattr(source, "fetch_offset_id", 0):
            state.fresh_until_at = progress.observed_at
            state.next_probe_at = now_value
    wake_subscribers(session, source, state, reason="channel_source_snapshot_ready")


def _persist_snapshot_messages(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    state: ListenerSourceState,
    snapshots: list[Any],
    snapshot_revision: int,
    binding: ChannelDiscussionGroupBinding | None,
    discussion_snapshot: Any | None,
    probe_request_id: str,
    observed_at: Any,
) -> None:
    scope = ListenerChannelSnapshotItem.listener_source_state_id == state.id
    if getattr(source, "fetch_offset_id", 0):
        session.execute(update(ListenerChannelSnapshotItem).where(scope).values(snapshot_revision=snapshot_revision))
    else:
        session.execute(delete(ListenerChannelSnapshotItem).where(scope))
    for snapshot in snapshots:
        thread_root_id = _discussion_root_id(discussion_snapshot, snapshot.message_id)
        message, revision_created = _upsert_channel_message(
            session, source, channel,
            snapshot=snapshot,
            observed_at=observed_at,
            binding=binding,
            thread_root_id=thread_root_id,
        )
        if message is None:
            continue
        _freeze_message_discussion_identity(
            session, message, binding=binding,
            thread_root_id=thread_root_id,
            revision_created=revision_created,
            probe_request_id=probe_request_id,
            observed_at=observed_at,
            freshness_seconds=source.collect_window_seconds * FRESHNESS_MULTIPLIER,
        )
        session.flush()
        session.add(ListenerChannelSnapshotItem(
            listener_source_state_id=state.id,
            snapshot_revision=snapshot_revision,
            channel_message_id=message.id,
        ))


def _mark_snapshot_ready(
    state: ListenerSourceState,
    source: Any,
    *,
    next_revision: int,
    now_value: Any,
) -> None:
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


def _persist_discussion_binding(
    session: Session,
    source: Any,
    channel: OperationTarget,
    *,
    probe_request_id: str,
    snapshot: Any | None,
    error_code: str,
    observed_at: Any,
) -> ChannelDiscussionGroupBinding | None:
    discussion_peer = str(getattr(snapshot, "discussion_peer_id", "") or "") or None
    target = _ensure_discussion_target(
        session, source,
        discussion_peer=discussion_peer,
        discussion_title=str(getattr(snapshot, "discussion_title", "") or ""),
    )
    observation = GroupProbeObservation(
        tenant_id=source.tenant_id,
        channel_target_id=channel.id,
        target_reference_revision=channel.reference_revision,
        channel_peer_id=channel.tg_peer_id,
        discussion_target_id=target.id if target else None,
        discussion_peer_id=discussion_peer,
        probe_request_id=probe_request_id,
        probe_status="success" if snapshot is not None else "probe_failed",
        probe_stage=AUTHORITATIVE_GROUP_STAGE,
        account_id=source.account_id,
        observed_at=_wall(observed_at),
        fresh_until_at=_wall(observed_at) + timedelta(seconds=source.collect_window_seconds * FRESHNESS_MULTIPLIER),
        error_code=error_code,
    )
    return record_group_probe(session, observation)


def _ensure_discussion_target(
    session: Session,
    source: Any,
    *,
    discussion_peer: str | None,
    discussion_title: str,
) -> OperationTarget | None:
    if not discussion_peer:
        return None
    target = session.scalar(select(OperationTarget).where(
        OperationTarget.tenant_id == source.tenant_id,
        OperationTarget.tg_peer_id == discussion_peer,
    ))
    if target is not None:
        return target
    target = OperationTarget(
        tenant_id=source.tenant_id,
        target_type="group",
        tg_peer_id=discussion_peer,
        title=discussion_title or f"频道讨论组 {discussion_peer}",
        auth_status="Telegram权威发现",
    )
    session.add(target)
    session.flush()
    return target


def _freeze_message_discussion_identity(
    session: Session,
    message: ChannelMessage,
    *,
    binding: ChannelDiscussionGroupBinding | None,
    thread_root_id: int,
    revision_created: bool,
    probe_request_id: str,
    observed_at: Any,
    freshness_seconds: int,
) -> None:
    source_revision = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
    if source_revision is None or binding is None:
        return
    if not _freeze_group_identity(source_revision, binding):
        return
    if binding.binding_status != "active":
        return
    thread = record_thread_probe(session, ThreadProbeObservation(
        tenant_id=message.tenant_id,
        source_revision_id=source_revision.id,
        group_binding_id=binding.id,
        probe_request_id=f"{probe_request_id}:{message.message_id}",
        probe_status="success" if thread_root_id else "probe_failed",
        probe_stage=AUTHORITATIVE_THREAD_STAGE,
        observed_at=_wall(observed_at),
        fresh_until_at=_wall(observed_at) + timedelta(seconds=freshness_seconds),
        discussion_peer_id=binding.discussion_peer_id,
        thread_root_message_id=thread_root_id or None,
        error_code="" if thread_root_id else "discussion_thread_root_missing",
    ))
    if thread is None:
        return
    _freeze_thread_identity(
        source_revision, thread, allow_initialization=revision_created,
    )


def _freeze_group_identity(source_revision, binding) -> bool:
    frozen = (
        source_revision.discussion_group_binding_id,
        source_revision.discussion_group_binding_revision,
        source_revision.discussion_group_identity_hash,
    )
    observed = (binding.id, binding.binding_revision, binding.identity_hash)
    return frozen == observed


def _freeze_thread_identity(source_revision, thread, *, allow_initialization: bool) -> bool:
    frozen = (
        source_revision.discussion_thread_binding_id,
        source_revision.discussion_thread_revision,
        source_revision.discussion_thread_identity_hash,
    )
    observed = (thread.id, thread.thread_revision, thread.identity_hash)
    if any(frozen):
        return frozen == observed
    if not allow_initialization:
        return False
    source_revision.discussion_thread_binding_id = thread.id
    source_revision.discussion_thread_revision = thread.thread_revision
    source_revision.discussion_thread_identity_hash = thread.identity_hash
    return True


def _discussion_root_id(discussion_snapshot: Any | None, message_id: int) -> int:
    if discussion_snapshot is None:
        return 0
    roots = discussion_snapshot.thread_root_by_source_message_id
    return int(roots.get(int(message_id)) or 0)


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
        message.source_metadata = {**dict(message.source_metadata or {}), "deleted": True}
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
    if message.published_at and published_at and message.published_at != published_at:
        raise RuntimeError("source_published_at_conflict")
    revision, revision_created = _append_source_revision(
        session, source, message, snapshot=snapshot,
        published_at=published_at, observed_at=observed_at,
        binding=binding, thread_root_id=thread_root_id,
    )
    message.message_url = snapshot.message_url or message.message_url or _message_url(channel, snapshot.message_id)
    message.content_preview = snapshot.content_preview or message.content_preview
    message.grouped_id = str(getattr(snapshot, "grouped_id", "") or "")
    message.source_metadata = dict(getattr(snapshot, "source_metadata", {}) or {})
    message.comment_available = bool(snapshot.comment_available)
    message.published_at = published_at or message.published_at
    if revision is not None:
        message.current_source_revision_id = revision.id
        if revision.source_operation == "edited":
            reconcile_channel_comment_source_edit(
                session, message, revision, at=_wall(observed_at),
            )
    return message, revision_created


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
        source_operation=_source_operation(current, content_hash, edited_at),
    )
    session.add(revision)
    session.flush()
    return revision, True


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
        source_published_at=published_at,
        source_published_at_fact_id=(
            f"telegram_message_date:{message.channel_target_id}:{int(snapshot.message_id)}"
        ),
        telegram_edit_date=edited_at,
        source_observed_at=_wall(observed_at),
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


def _wall(value):
    return value.replace(tzinfo=None) if value.tzinfo else value
