from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select

from app.models import TgAccount, TgAccountAuthorization
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneSourceEvent,
    CloneSourceStreamState,
)
from app.services._common import gateway
from app.services.developer_apps import credentials_for_authorization

QUOTE_PREVIEW_CHARS = 200
WAITING_PARENT_STATES = {
    "observed", "waiting_source_base", "waiting_binding", "waiting_album",
    "waiting_dependency", "waiting_transport", "ready", "action_bound",
    "executing", "unknown_after_send", "remote_reconcile_only",
}


@dataclass(frozen=True)
class ReplyResolution:
    target_message_id: int | None = None
    quote_prefix: str = ""
    parent_sender_peer_id: str | None = None
    terminal_state: str = ""
    error_code: str = ""


def resolve_orphan_reply(session, task, *, config, event, sanitize):
    if not event.reply_to_message_id:
        return ReplyResolution()
    parent = _parent_obligation(session, task, event.reply_to_message_id)
    if parent and parent.state in WAITING_PARENT_STATES:
        return ReplyResolution(terminal_state="waiting_dependency", error_code="reply_parent_not_terminal")
    policy = config.content.orphan_reply_policy
    if policy == "drop_subtree":
        return ReplyResolution(terminal_state="filtered", error_code="orphan_reply_drop_subtree")
    if policy == "block_for_review":
        return ReplyResolution(terminal_state="waiting_manual_review", error_code="orphan_reply_review_required")
    return _fresh_quote(session, task, event=event, sanitize=sanitize)


def _fresh_quote(session, task, *, event, sanitize):
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ))
    authorization = session.get(TgAccountAuthorization, stream.authorization_id) if stream else None
    account = session.get(TgAccount, stream.listener_account_id) if stream else None
    if authorization is None or account is None:
        return _review("reply_lazy_fetch_authorization_missing")
    try:
        snapshot = gateway.fetch_group_message(
            account.id,
            stream.source_peer_id,
            str(event.reply_to_message_id),
            session_ciphertext=authorization.session_ciphertext,
            credentials=credentials_for_authorization(session, authorization),
        )
    except Exception as exc:
        return _review(f"reply_lazy_fetch_failed:{type(exc).__name__}")
    if snapshot is None or int(snapshot.remote_message_id or 0) != event.reply_to_message_id:
        return _review("reply_lazy_fetch_identity_unproven")
    parent = SimpleNamespace(
        content=snapshot.content or snapshot.caption or "",
        sender_peer_id=snapshot.sender_peer_id,
        media_type=snapshot.media_type or "text",
        entities=[],
    )
    sanitized = sanitize(parent)
    if not sanitized:
        return _review("reply_lazy_fetch_sanitization_rejected")
    excerpt = sanitized[:QUOTE_PREVIEW_CHARS].strip()
    if not excerpt:
        return _review("reply_lazy_fetch_empty_after_sanitization")
    return ReplyResolution(
        quote_prefix=f"> {excerpt}\n\n",
        parent_sender_peer_id=snapshot.sender_peer_id or None,
        error_code="orphan_reply_quote_fallback",
    )


def _parent_obligation(session, task, source_message_id):
    return session.scalar(
        select(CloneDeliveryObligation)
        .join(CloneSourceEvent, CloneSourceEvent.id == CloneDeliveryObligation.source_event_id)
        .where(
            CloneDeliveryObligation.task_id == task.id,
            CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
            CloneSourceEvent.source_message_id == source_message_id,
        )
        .order_by(CloneSourceEvent.message_revision.desc())
        .limit(1)
    )


def _review(code):
    return ReplyResolution(terminal_state="waiting_manual_review", error_code=code)


__all__ = ["ReplyResolution", "resolve_orphan_reply"]
