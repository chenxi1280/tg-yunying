"""Promote repeated bot rules only when correlated with a pending send probe."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, PendingVisibilityCredit, TgGroup
from app.timezone import as_beijing, beijing_now

from .group_bot_global_rules import (
    apply_trusted_global_group_rule,
    is_repeatable_recipient_channel_rule,
)


POST_SEND_RULE_CORRELATION_SECONDS = 180
POST_SEND_RULE_HOLD_LIMIT = 20


def apply_correlated_post_send_rule(
    session: Session,
    group: TgGroup,
    *,
    remote_id: str,
    content: str,
    bot_peer: str,
    is_admin_bot: bool,
    controls: list[dict[str, object]],
) -> bool:
    if not _has_correlated_visibility_hold(session, group_id=int(group.id), bot_message_id=remote_id):
        return False
    if not is_repeatable_recipient_channel_rule(
        session,
        tenant_id=group.tenant_id,
        group_id=int(group.id),
        message_id=remote_id,
        bot_peer_id=bot_peer,
        text=content,
        control_buttons=controls,
    ):
        return False
    applied = apply_trusted_global_group_rule(
        session,
        tenant_id=group.tenant_id,
        group_id=int(group.id),
        message_id=remote_id,
        text=content,
        bot_peer_id=bot_peer,
        is_admin_bot=is_admin_bot,
        control_buttons=controls,
        evidence_kind="post_send_intercept_rule",
    )
    return bool(applied)


def _has_correlated_visibility_hold(
    session: Session,
    *,
    group_id: int,
    bot_message_id: str,
) -> bool:
    try:
        bot_id = int(bot_message_id)
    except (TypeError, ValueError):
        return False
    rows = session.execute(
        select(PendingVisibilityCredit, Action)
        .join(Action, Action.id == PendingVisibilityCredit.action_id)
        .where(
            PendingVisibilityCredit.status == "open",
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
        )
        .order_by(PendingVisibilityCredit.created_at.desc())
        .limit(POST_SEND_RULE_HOLD_LIMIT)
    )
    now = beijing_now()
    for hold, action in rows:
        if _hold_matches(
            hold,
            action,
            group_id=group_id,
            bot_message_id=bot_id,
            now=now,
        ):
            return True
    return False


def _hold_matches(
    hold: PendingVisibilityCredit,
    action: Action,
    *,
    group_id: int,
    bot_message_id: int,
    now: datetime,
) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if int(payload.get("group_id") or 0) != group_id:
        return False
    created_at = as_beijing(hold.created_at)
    age = now - created_at if created_at else timedelta.max
    if age < timedelta(0) or age > timedelta(seconds=POST_SEND_RULE_CORRELATION_SECONDS):
        return False
    try:
        return bot_message_id > int(hold.remote_message_id)
    except (TypeError, ValueError):
        return False
