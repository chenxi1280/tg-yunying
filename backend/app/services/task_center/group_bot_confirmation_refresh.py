"""Refresh a trusted group-bot callback from the live Telegram message before click."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GroupBotAdmission, GroupContextMessage, TgAccount, TgGroup
from app.services.group_context_messages import try_insert_context_message

from .group_bot_admission import (
    confirmation_button,
    current_required_channel_refs,
    is_group_bot_control_prompt,
    parse_channel_refs,
)
from .payloads import GroupBotConfirmationButtonPayload


MAX_CONTEXT_CONTENT_LENGTH = 4000


@dataclass(frozen=True)
class LiveConfirmationRefreshContext:
    action: Action
    admission: GroupBotAdmission
    group: TgGroup
    account: TgAccount
    credentials: object
    gateway_client: object
    payload: GroupBotConfirmationButtonPayload


class LiveConfirmationSourceFetchError(RuntimeError):
    pass


def refresh_live_confirmation_source(
    session: Session,
    context: LiveConfirmationRefreshContext,
) -> GroupBotConfirmationButtonPayload | None:
    snapshot = _latest_matching_snapshot(context)
    if snapshot is None:
        return None
    controls = list(getattr(snapshot, "control_buttons", ()) or ())
    button = confirmation_button(controls)
    if button is None:
        return None
    _record_live_snapshot(session, context, snapshot, controls)
    return _apply_live_source(context, snapshot, button)


def _latest_matching_snapshot(context: LiveConfirmationRefreshContext):
    snapshots = _fetch_live_snapshots(context)
    for snapshot in snapshots:
        if _matches_trusted_confirmation_prompt(context, snapshot):
            return snapshot
    return None


def _fetch_live_snapshots(context: LiveConfirmationRefreshContext) -> list[object]:
    try:
        return list(
            context.gateway_client.fetch_group_messages(
                context.account.id,
                context.group.tg_peer_id,
                context.account.session_ciphertext,
                context.credentials,
                limit=int(context.group.listener_context_limit),
            )
        )
    except Exception as exc:  # noqa: BLE001 - caller records the Telegram source refresh failure explicitly.
        raise LiveConfirmationSourceFetchError(str(exc) or "group bot live source fetch failed") from exc


def _matches_trusted_confirmation_prompt(context: LiveConfirmationRefreshContext, snapshot: object) -> bool:
    controls = list(getattr(snapshot, "control_buttons", ()) or ())
    if not str(getattr(snapshot, "remote_message_id", "") or ""):
        return False
    if not bool(getattr(snapshot, "is_bot", False)):
        return False
    if str(getattr(snapshot, "sender_peer_id", "") or "") != context.payload.trusted_bot_peer_id:
        return False
    if not is_group_bot_control_prompt(str(getattr(snapshot, "content", "") or ""), controls):
        return False
    expected_refs = {item.casefold() for item in current_required_channel_refs(context.admission)}
    observed_refs = {
        item.casefold()
        for item in parse_channel_refs(str(getattr(snapshot, "content", "") or ""), controls)
    }
    return expected_refs == observed_refs and confirmation_button(controls) is not None


def _record_live_snapshot(
    session: Session,
    context: LiveConfirmationRefreshContext,
    snapshot: object,
    controls: list[object],
) -> None:
    remote_message_id = str(getattr(snapshot, "remote_message_id", "") or "")
    existing = session.scalar(
        select(GroupContextMessage).where(
            GroupContextMessage.group_id == context.group.id,
            GroupContextMessage.remote_message_id == remote_message_id,
        )
    )
    serialized_controls = [_serialized_control(item) for item in controls]
    if existing is not None:
        existing.control_buttons = serialized_controls
        return
    inserted = try_insert_context_message(
        session,
        GroupContextMessage(
            tenant_id=context.action.tenant_id,
            group_id=context.group.id,
            listener_account_id=context.account.id,
            sender_peer_id=str(getattr(snapshot, "sender_peer_id", "") or ""),
            sender_name=str(getattr(snapshot, "sender_name", "群管机器人") or "群管机器人"),
            sender_username=str(getattr(snapshot, "sender_username", "") or "").lstrip("@"),
            is_bot=True,
            sender_role=str(getattr(snapshot, "sender_role", "member") or "member"),
            content=str(getattr(snapshot, "content", "") or "")[:MAX_CONTEXT_CONTENT_LENGTH],
            message_type=str(getattr(snapshot, "message_type", "text") or "text"),
            remote_message_id=remote_message_id,
            control_buttons=serialized_controls,
            sent_at=getattr(snapshot, "sent_at", None),
        ),
    )
    if inserted:
        return
    duplicate = session.scalar(
        select(GroupContextMessage).where(
            GroupContextMessage.group_id == context.group.id,
            GroupContextMessage.remote_message_id == remote_message_id,
        )
    )
    if duplicate is not None:
        duplicate.control_buttons = serialized_controls


def _serialized_control(button: object) -> dict[str, object]:
    value = button.get if isinstance(button, dict) else lambda field, default="": getattr(button, field, default)
    return {
        "row": int(value("row", 0) or 0),
        "col": int(value("col", 0) or 0),
        "text": str(value("text", "") or ""),
        "url": str(value("url", "") or ""),
        "action_type": str(value("action_type", "other") or "other"),
    }


def _apply_live_source(
    context: LiveConfirmationRefreshContext,
    snapshot: object,
    button: dict[str, object],
) -> GroupBotConfirmationButtonPayload:
    source_message_id = str(getattr(snapshot, "remote_message_id", "") or "")
    refreshed_data = {
        **context.payload.model_dump(mode="json"),
        "source_message_id": source_message_id,
        "button_row": int(button["row"]),
        "button_col": int(button["col"]),
        "button_text": str(button["text"]),
    }
    refreshed = GroupBotConfirmationButtonPayload.model_validate(refreshed_data)
    changed = context.action.payload != refreshed_data
    context.action.payload = refreshed_data
    context.admission.source_message_id = source_message_id
    if changed:
        context.action.result = {
            **(context.action.result or {}),
            "group_bot_confirmation_source_refresh": {
                "from": context.payload.source_message_id,
                "to": source_message_id,
                "button": {"row": refreshed.button_row, "col": refreshed.button_col, "text": refreshed.button_text},
                "source": "live_group_fetch",
            },
        }
    return refreshed


__all__ = [
    "LiveConfirmationRefreshContext",
    "LiveConfirmationSourceFetchError",
    "refresh_live_confirmation_source",
]
