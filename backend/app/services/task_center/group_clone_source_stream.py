from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Task, TgAccount, TgAccountAuthorization
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now, gateway
from app.services.developer_apps import credentials_for_authorization
from .group_mutation_authority import release_exclusive_authority

logger = logging.getLogger(__name__)


class NormalizedCloneItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_message_id: int = Field(gt=0)
    event_type: str
    sender_peer_type: str | None = None
    sender_peer_id: str | None = None
    reply_to_message_id: int | None = None
    source_top_message_id: int | None = None
    grouped_id: str | None = None
    media_type: str | None = None
    content: str = ""
    entities: list[dict] = Field(default_factory=list)
    poll_snapshot: dict = Field(default_factory=dict)
    protected_content: bool = False
    message_revision: int = Field(default=1, ge=1)


def advance_group_clone_start(session: Session, task: Task) -> bool:
    if task.type != "group_clone" or _start_state(task) != "starting":
        return False
    stream = _stream(session, task, lock=False)
    if stream is None:
        raise RuntimeError("group_clone_start_stream_missing")
    account = session.get(TgAccount, stream.listener_account_id)
    authorization = session.get(TgAccountAuthorization, stream.authorization_id)
    update_state = session.get(TelegramAuthorizationUpdateState, stream.authorization_update_state_id)
    _validate_listener_runtime(account, authorization, update_state)
    try:
        boundary = gateway.fetch_raw_channel_boundary(
            stream.source_peer_id,
            session_ciphertext=authorization.session_ciphertext,
            credentials=credentials_for_authorization(session, authorization),
        )
        _apply_start_boundary(session, task, boundary)
        consume_clone_deliveries(session, task)
    except Exception as exc:
        _fail_clone_start(session, task, stream=stream, exc=exc)
        return False
    return task.status == "running"


def _fail_clone_start(session, task, *, stream, exc) -> None:
    detail = str(exc).strip() or type(exc).__name__
    logger.exception("group clone start failed task_id=%s", task.id)
    stream.state = "blocked"
    subscription = _subscription(session, task, lock=True)
    subscription.state = "stopped"
    task.status = "failed"
    task.last_error = detail
    task.stats = {**dict(task.stats or {}), "clone_start_state": "start_failed", "clone_start_error": detail}
    target = (task.type_config or {}).get("target", {})
    release_exclusive_authority(
        session,
        task.tenant_id,
        target_peer_type=str(target.get("peer_type") or ""),
        target_peer_id=str(target.get("peer_id") or ""),
        writer_kind="group_clone",
        writer_id=task.id,
    )


def consume_clone_deliveries(session: Session, task: Task, *, limit: int = 200) -> int:
    stream = _stream(session, task, lock=True)
    if stream is None or stream.state not in {"catching_up", "live"}:
        return 0
    rows = session.execute(
        select(TelegramAuthorizationUpdateDelivery, TelegramAuthorizationUpdateEvent)
        .join(
            TelegramAuthorizationUpdateEvent,
            TelegramAuthorizationUpdateEvent.id == TelegramAuthorizationUpdateDelivery.update_event_id,
        )
        .join(
            TelegramAuthorizationUpdateSubscription,
            TelegramAuthorizationUpdateSubscription.id == TelegramAuthorizationUpdateDelivery.subscription_id,
        )
        .where(
            TelegramAuthorizationUpdateDelivery.task_id == task.id,
            TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
            TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
        )
        .order_by(
            TelegramAuthorizationUpdateEvent.ingress_order_no,
            TelegramAuthorizationUpdateDelivery.normalized_item_index,
        )
        .limit(limit)
        .with_for_update()
    ).all()
    consumed = 0
    for delivery, envelope in rows:
        if not _consume_delivery(session, task, stream=stream, delivery=delivery, envelope=envelope):
            break
        consumed += 1
    _promote_live_if_caught_up(session, task, stream)
    return consumed


def _apply_start_boundary(session, task, boundary) -> None:
    channel_pts = int(boundary.get("channel_pts") or 0)
    max_message_id = int(boundary.get("max_message_id") or 0)
    if channel_pts <= 0 or max_message_id < 0:
        raise RuntimeError("group_clone_start_boundary_unproven")
    stream = _stream(session, task, lock=True)
    subscription = _subscription(session, task, lock=True)
    update_state = session.get(TelegramAuthorizationUpdateState, stream.authorization_update_state_id)
    if (
        update_state is None
        or update_state.state != "live"
        or not update_state.owner_id
        or update_state.lease_expires_at is None
        or update_state.lease_expires_at <= _now()
    ):
        raise RuntimeError("group_clone_shared_ingress_not_live")
    stream.channel_pts = channel_pts
    stream.start_pts = channel_pts
    stream.start_message_id = max_message_id
    stream.difference_cursor = {"start_message_id": max_message_id, "start_channel_pts": channel_pts}
    stream.state = "catching_up"
    stream.version = int(stream.version or 1) + 1
    subscription.state = "active"
    subscription.version = int(subscription.version or 1) + 1


def _consume_delivery(session, task, *, stream, delivery, envelope) -> bool:
    item = NormalizedCloneItem.model_validate(delivery.normalized_payload or {})
    if not _pts_continuous(stream, envelope):
        stream.state = "gap"
        task.status = "failed"
        task.last_error = "group_clone_source_pts_gap"
        task.stats = {
            **dict(task.stats or {}),
            "clone_start_state": "runtime_blocked",
            "clone_gap_at": envelope.ingress_order_no,
        }
        return False
    stream.channel_pts = max(stream.channel_pts, int(envelope.pts_evidence or 0))
    stream.last_consumed_ingress_order_no = envelope.ingress_order_no
    if _before_boundary(stream, item):
        delivery.delivery_state = "skipped"
        return True
    identity = _event_identity(stream, item, delivery)
    existing = session.scalar(select(CloneSourceEvent.id).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.event_identity_hash == identity,
    ))
    if existing is None:
        stream.last_applied_stream_order_no += 1
        session.add(_source_event(
            task, stream, envelope=envelope, delivery=delivery, item=item, identity=identity,
        ))
    delivery.delivery_state = "consumed"
    stream.last_applied_event_hash = identity
    return True


def _source_event(task, stream, *, envelope, delivery, item, identity):
    return CloneSourceEvent(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        source_peer_type=stream.source_peer_type,
        source_peer_id=stream.source_peer_id,
        source_message_id=item.source_message_id,
        event_type=item.event_type,
        ingress_update_identity_hash=envelope.update_identity_hash,
        event_identity_hash=identity,
        source_pts=envelope.pts_evidence,
        source_pts_count=envelope.pts_count_evidence,
        authorization_ingress_order_no=envelope.ingress_order_no,
        normalized_item_index=delivery.normalized_item_index,
        apply_order_key=f"{envelope.ingress_order_no:020d}:{delivery.normalized_item_index:06d}",
        stream_order_no=stream.last_applied_stream_order_no,
        message_revision=item.message_revision,
        sender_peer_type=item.sender_peer_type,
        sender_peer_id=item.sender_peer_id,
        reply_to_message_id=item.reply_to_message_id,
        source_top_message_id=item.source_top_message_id,
        grouped_id=item.grouped_id,
        media_type=item.media_type,
        content=item.content,
        entities=item.entities,
        poll_snapshot=item.poll_snapshot,
        content_fingerprint=delivery.payload_fingerprint,
        protected_content=item.protected_content,
        config_revision=task.config_revision,
    )


def _promote_live_if_caught_up(session, task, stream) -> None:
    pending = session.scalar(
        select(TelegramAuthorizationUpdateDelivery.id)
        .join(
            TelegramAuthorizationUpdateSubscription,
            TelegramAuthorizationUpdateSubscription.id == TelegramAuthorizationUpdateDelivery.subscription_id,
        )
        .where(
            TelegramAuthorizationUpdateDelivery.task_id == task.id,
            TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
            TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
        )
        .limit(1)
    )
    if pending or stream.state != "catching_up":
        return
    stream.state = "live"
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}


def _pts_continuous(stream, envelope) -> bool:
    pts = int(envelope.pts_evidence or 0)
    count = int(envelope.pts_count_evidence or 0)
    if pts <= 0 or count <= 0:
        return False
    return pts - count <= int(stream.channel_pts or 0)


def _before_boundary(stream, item) -> bool:
    start_message_id = int((stream.difference_cursor or {}).get("start_message_id") or 0)
    return item.source_message_id <= start_message_id


def _event_identity(stream, item, delivery) -> str:
    raw = {
        "source_peer_type": stream.source_peer_type,
        "source_peer_id": stream.source_peer_id,
        "event_type": item.event_type,
        "source_message_id": item.source_message_id,
        "message_revision": item.message_revision,
        "source_top_message_id": item.source_top_message_id,
        "grouped_id": item.grouped_id,
        "payload_fingerprint": delivery.payload_fingerprint,
    }
    value = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _stream(session, task, *, lock):
    stmt = select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    )
    return session.scalar(stmt.with_for_update() if lock else stmt)


def _subscription(session, task, *, lock):
    stmt = select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
    )
    row = session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise RuntimeError("group_clone_update_subscription_missing")
    return row


def _validate_listener_runtime(account, authorization, update_state) -> None:
    if account is None or authorization is None or update_state is None:
        raise RuntimeError("group_clone_listener_runtime_missing")
    if account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        raise RuntimeError("group_clone_listener_account_not_online")
    if not authorization.is_current or authorization.status != "active":
        raise RuntimeError("group_clone_listener_authorization_stale")
    if authorization.slot_generation != update_state.session_generation:
        raise RuntimeError("group_clone_listener_generation_mismatch")


def _start_state(task) -> str:
    return str((task.stats or {}).get("clone_start_state") or "")


__all__ = ["advance_group_clone_start", "consume_clone_deliveries"]
