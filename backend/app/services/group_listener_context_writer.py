from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupContextMessage, TgAccount, TgGroup

from .group_context_messages import try_insert_context_message
from .required_channel_prompts import apply_required_channel_prompt_admission
from .source_media import ensure_source_media_asset
from .tenant_learning_samples import record_group_learning_sample as record_tenant_group_learning_sample


def insert_context_snapshots(
    session: Session,
    group: TgGroup,
    account: TgAccount,
    snapshots: Iterable,
    *,
    ignored_sender: Callable[[object], bool],
    create_source_media: bool,
    learning_scene: str | None,
) -> int:
    inserted = 0
    for snapshot in snapshots:
        message = _context_message(session, group, account, snapshot, ignored_sender=ignored_sender, learning_scene=learning_scene)
        if message is None or not try_insert_context_message(session, message):
            continue
        apply_required_channel_prompt_admission(
            session,
            group,
            message.content,
            remote_message_id=message.remote_message_id,
        )
        if create_source_media and message.message_type != "text":
            _ensure_source_media(session, group, account, snapshot, message)
        inserted += 1
    return inserted


def _context_message(
    session: Session,
    group: TgGroup,
    account: TgAccount,
    snapshot,
    *,
    ignored_sender: Callable[[object], bool],
    learning_scene: str | None,
) -> GroupContextMessage | None:
    content = str(snapshot.content or "").strip()
    if not content:
        return None
    if learning_scene:
        record_tenant_group_learning_sample(session, group, snapshot)
    if ignored_sender(snapshot) or _message_exists(session, group.id, str(snapshot.remote_message_id)):
        return None
    return GroupContextMessage(
        tenant_id=group.tenant_id,
        group_id=group.id,
        listener_account_id=account.id,
        sender_peer_id=str(snapshot.sender_peer_id or ""),
        sender_name=str(snapshot.sender_name or "真人用户"),
        sender_username=str(getattr(snapshot, "sender_username", "") or "").lstrip("@"),
        is_bot=bool(getattr(snapshot, "is_bot", False)),
        sender_role=str(getattr(snapshot, "sender_role", "") or "member"),
        content=content[:4000],
        message_type=snapshot.message_type,
        remote_message_id=str(snapshot.remote_message_id),
        sent_at=snapshot.sent_at,
    )


def _message_exists(session: Session, group_id: int, remote_message_id: str) -> bool:
    return bool(
        session.scalar(
            select(GroupContextMessage.id).where(
                GroupContextMessage.group_id == group_id,
                GroupContextMessage.remote_message_id == remote_message_id,
            )
        )
    )


def _ensure_source_media(
    session: Session,
    group: TgGroup,
    account: TgAccount,
    snapshot,
    message: GroupContextMessage,
) -> None:
    ensure_source_media_asset(
        session,
        tenant_id=group.tenant_id,
        source_group_id=group.id,
        listener_account_id=account.id,
        source_peer_id=group.tg_peer_id,
        source_message_id=message.remote_message_id,
        source_media_group_id=str(getattr(snapshot, "media_group_id", "") or ""),
        media_group_index=int(getattr(snapshot, "media_group_index", 0) or 0),
        media_group_total=int(getattr(snapshot, "media_group_total", 1) or 1),
        media_type=str(getattr(snapshot, "media_type", "") or snapshot.message_type or "media"),
        caption=str(getattr(snapshot, "caption", "") or message.content),
        media_fingerprint=str(getattr(snapshot, "media_fingerprint", "") or ""),
    )
