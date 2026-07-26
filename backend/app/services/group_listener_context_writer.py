from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupBotAdmission, GroupContextMessage, TgAccount, TgGroup

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
        # Control-event path runs before context dedupe / ignore / learning filters.
        _process_group_bot_control_event(session, group, snapshot)
        _refresh_existing_control_buttons(session, group, snapshot)
        _record_speaker_event(session, group, snapshot, account=account)
        message = _context_message(
            session,
            group,
            account,
            snapshot,
            ignored_sender=ignored_sender,
            learning_scene=learning_scene,
        )
        # PRD: group-bot admission is independent of can_send. Skip legacy helper that
        # rewrites TgGroupAccount.can_send whenever any admission row exists for this group.
        has_group_bot_admission = session.scalar(
            select(GroupBotAdmission.id).where(
                GroupBotAdmission.tenant_id == group.tenant_id,
                GroupBotAdmission.group_id == group.id,
            ).limit(1)
        ) is not None
        if message is None or not try_insert_context_message(session, message):
            _maybe_apply_legacy_required_channel_prompt(session, group, snapshot, has_group_bot_admission)
            continue
        if not has_group_bot_admission:
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


def _maybe_apply_legacy_required_channel_prompt(session: Session, group: TgGroup, snapshot, has_admission: bool) -> None:
    content = str(getattr(snapshot, "content", "") or "").strip()
    if not content or has_admission:
        return
    apply_required_channel_prompt_admission(
        session,
        group,
        content,
        remote_message_id=str(getattr(snapshot, "remote_message_id", "") or ""),
    )


def _refresh_existing_control_buttons(session: Session, group: TgGroup, snapshot) -> None:
    controls = _control_button_summaries(snapshot)
    peer_id = str(getattr(snapshot, "sender_peer_id", "") or "")
    remote_id = str(getattr(snapshot, "remote_message_id", "") or "")
    if not bool(getattr(snapshot, "is_bot", False)) or not controls or not peer_id or not remote_id:
        return
    existing = session.scalar(
        select(GroupContextMessage).where(
            GroupContextMessage.group_id == group.id,
            GroupContextMessage.remote_message_id == remote_id,
        )
    )
    if (
        existing is None
        or not existing.is_bot
        or existing.sender_peer_id != peer_id
        or existing.control_buttons
    ):
        return
    existing.control_buttons = controls
    session.flush()


def _process_group_bot_control_event(session: Session, group: TgGroup, snapshot) -> None:
    from app.services.task_center.group_bot_admission import (
        is_group_bot_control_prompt,
        is_group_bot_completion_event,
        is_trusted_group_bot_source,
    )

    event = _bot_control_event(snapshot)
    if event is None:
        return
    remote_id, content, bot_peer, is_admin_bot = event
    if not is_trusted_group_bot_source(
        session,
        tenant_id=group.tenant_id,
        group_id=group.id,
        bot_peer_id=bot_peer,
        is_admin_bot=is_admin_bot,
    ):
        return
    controls = _control_button_summaries(snapshot)
    button_confirmed = bool(getattr(snapshot, "button_confirmed", False))
    if not (
        is_group_bot_control_prompt(content, controls)
        or is_group_bot_completion_event(content, button_confirmed=button_confirmed)
    ):
        return
    waiting = _waiting_group_bot_admissions(session, group)
    target_id, reason = _attributed_waiting_account(session, waiting, content)
    if target_id is None:
        return
    _apply_trusted_group_bot_control(
        session,
        group,
        snapshot,
        target_id=int(target_id),
        attribution_reason=reason,
        remote_id=remote_id,
        content=content,
        bot_peer=bot_peer,
        is_admin_bot=is_admin_bot,
        control_buttons=controls,
        button_confirmed=button_confirmed,
    )


def _bot_control_event(snapshot) -> tuple[str, str, str, bool] | None:
    remote_id = str(getattr(snapshot, "remote_message_id", "") or "")
    if not remote_id or not bool(getattr(snapshot, "is_bot", False)):
        return None
    sender_role = str(getattr(snapshot, "sender_role", "") or "member").lower()
    is_admin = sender_role in {"admin", "administrator", "creator", "owner"} or bool(
        getattr(snapshot, "sender_is_admin", False)
    )
    return remote_id, str(getattr(snapshot, "content", "") or ""), str(getattr(snapshot, "sender_peer_id", "") or ""), is_admin


def _apply_trusted_group_bot_control(
    session: Session,
    group: TgGroup,
    snapshot,
    *,
    target_id: int,
    attribution_reason: str,
    remote_id: str,
    content: str,
    bot_peer: str,
    is_admin_bot: bool,
    control_buttons: list[dict[str, object]],
    button_confirmed: bool,
) -> None:
    from app.services.task_center.group_bot_admission import (
        apply_confirmation_event,
        get_admission,
        ingest_trusted_bot_prompt,
        is_group_bot_completion_event,
        resolve_bound_task_id_for_group,
    )

    admission = get_admission(
        session,
        tenant_id=group.tenant_id,
        group_id=group.id,
        account_id=target_id,
    )
    if admission is None:
        return
    if is_group_bot_completion_event(content, button_confirmed=button_confirmed):
        apply_confirmation_event(
            session,
            admission=admission,
            message_id=remote_id,
            text=content,
            bot_peer_id=bot_peer,
            button_confirmed=button_confirmed,
        )
        admission.evidence_ref = f"attr:{attribution_reason};msg:{remote_id}"
        session.flush()
        return
    ingest_trusted_bot_prompt(
        session,
        admission=admission,
        message_id=remote_id,
        text=content,
        bot_peer_id=bot_peer,
        is_admin_bot=is_admin_bot,
        is_trusted_source=True,
        control_buttons=control_buttons,
        bound_task_id=resolve_bound_task_id_for_group(session, tenant_id=group.tenant_id, group_id=int(group.id)),
    )
    admission.evidence_ref = f"attr:{attribution_reason};msg:{remote_id}"
    session.flush()


def _waiting_group_bot_admissions(session: Session, group: TgGroup) -> list[GroupBotAdmission]:
    states = (
        "awaiting_group_bot_rule",
        "observation_open",
        "group_bot_policy_unresolved",
        "required_channel_follow_pending",
        "following_required_channel",
        "awaiting_group_bot_confirmation",
    )
    return list(
        session.scalars(
            select(GroupBotAdmission).where(
                GroupBotAdmission.tenant_id == group.tenant_id,
                GroupBotAdmission.group_id == group.id,
                GroupBotAdmission.state.in_(states),
            )
        )
    )


def _attributed_waiting_account(session: Session, waiting: list[GroupBotAdmission], text: str) -> tuple[int | None, str]:
    if not waiting:
        return None, "no_waiting_admission"
    account_ids = [int(item.account_id) for item in waiting]
    accounts = {
        int(row.id): row
        for row in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids))).all()
    }
    usernames = {account_id: str(accounts[account_id].username or "") for account_id in account_ids if account_id in accounts}
    names = {
        account_id: str(accounts[account_id].display_name or accounts[account_id].username or "")
        for account_id in account_ids
        if account_id in accounts
    }
    from app.services.task_center.group_bot_admission import attribute_prompt_to_account

    return attribute_prompt_to_account(
        text=text,
        waiting_account_ids=account_ids,
        account_usernames=usernames,
        account_display_names=names,
    )


def _record_speaker_event(session: Session, group: TgGroup, snapshot, *, account: TgAccount) -> None:
    from app.services.task_center.conversation_speaker_rotation import (
        CONTROL_KIND,
        HUMAN_KIND,
        conversation_key_for_group,
        record_conversation_event,
    )

    remote_id = str(getattr(snapshot, "remote_message_id", "") or "")
    if not remote_id:
        return
    is_bot = bool(getattr(snapshot, "is_bot", False))
    sender_role = str(getattr(snapshot, "sender_role", "") or "").lower()
    if is_bot or sender_role in {"admin", "administrator", "creator", "system", "service"}:
        kind = CONTROL_KIND if is_bot else "system"
    else:
        # Platform accounts are not tagged here; default to human for external senders.
        kind = HUMAN_KIND
    record_conversation_event(
        session,
        tenant_id=group.tenant_id,
        surface="group_ai_chat",
        conversation_key=conversation_key_for_group(group_id=int(group.id)),
        remote_message_id=remote_id,
        sender_kind=kind,
        remote_cursor=remote_id,
        account_id=None,
    )


def _context_message(
    session: Session,
    group: TgGroup,
    account: TgAccount,
    snapshot,
    *,
    ignored_sender: Callable[[object], bool],
    learning_scene: str | None,
) -> GroupContextMessage | None:
    content = str(getattr(snapshot, "content", "") or "").strip()
    is_control = bool(getattr(snapshot, "is_bot", False)) and _looks_like_group_bot_rule(content)
    controls = _control_button_summaries(snapshot)
    if not content and not controls:
        return None
    if learning_scene:
        # Always record for audit; tenant learning module rejects bots/managed accounts.
        record_tenant_group_learning_sample(session, group, snapshot)
    if (ignored_sender(snapshot) and not is_control) or _message_exists(session, group.id, str(snapshot.remote_message_id)):
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
        message_type=str(getattr(snapshot, "message_type", "text") or "text"),
        remote_message_id=str(snapshot.remote_message_id),
        control_buttons=controls,
        sent_at=snapshot.sent_at,
    )


def _control_button_summaries(snapshot) -> list[dict[str, object]]:
    controls: list[dict[str, object]] = []
    for button in getattr(snapshot, "control_buttons", ()) or ():
        row = _button_number(button, "row")
        col = _button_number(button, "col")
        if row is None or col is None:
            continue
        text = _button_text(button, "text")
        url = _button_text(button, "url")
        action_type = _button_text(button, "action_type")
        if action_type not in {"url", "callback", "other"}:
            action_type = "url" if url else "other"
        if text or url:
            controls.append({"row": row, "col": col, "text": text, "url": url, "action_type": action_type})
    return controls


def _button_text(button: object, field: str) -> str:
    value = button.get(field, "") if isinstance(button, dict) else getattr(button, field, "")
    return str("" if value is None else value).strip()


def _button_number(button: object, field: str) -> int | None:
    try:
        value = int(_button_text(button, field))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _looks_like_group_bot_rule(text: str) -> bool:
    lowered = (text or "").lower()
    markers = ("关注", "频道", "验证", "解禁", "发言", "follow", "channel", "unmute")
    return any(marker in text or marker in lowered for marker in markers)


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
