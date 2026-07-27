"""Apply an audited, recipient-free group-bot rule to scoped admissions only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupBotAdmission, GroupBotRequiredChannelFollow, GroupContextMessage, Task, TaskMembershipAdmissionItem

from .group_bot_admission import (
    SOURCE_BOUND_POLICY_TYPES,
    active_policy,
    confirmation_button,
    discard_repeatable_recipient_confirmation,
    ingest_trusted_bot_prompt,
    is_group_bot_control_prompt,
    parse_channel_refs,
)


GLOBAL_RULE_STATES = (
    "awaiting_group_bot_rule",
    "observation_open",
    "observation_stale",
    "group_bot_policy_unresolved",
    "group_bot_rule_unattributed",
    "required_channel_follow_pending",
    "following_required_channel",
    "awaiting_group_bot_confirmation",
)
REPEATABLE_RECIPIENT_RULE_CONTEXT_LIMIT = 100


def apply_trusted_global_group_rule(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    message_id: str,
    text: str,
    bot_peer_id: str,
    is_admin_bot: bool,
    control_buttons: list[dict[str, object]],
    evidence_kind: str = "trusted_global_rule",
) -> list[GroupBotAdmission]:
    """Plan exact admission actions for a trusted group-wide channel rule."""
    if not _global_rule_authorized(session, tenant_id, group_id, bot_peer_id, is_admin_bot):
        return []
    channel_refs = tuple(parse_channel_refs(text, control_buttons))
    if not is_group_bot_control_prompt(text, control_buttons) or not channel_refs:
        return []
    applied: list[GroupBotAdmission] = []
    follow_only = evidence_kind == "trusted_repeatable_recipient_rule"
    for admission in _candidate_admissions(session, tenant_id, group_id, bot_peer_id):
        task_id = _bound_running_task_id(session, admission)
        if not task_id:
            continue
        if follow_only and admission.evidence_ref.startswith("attr:trusted_repeatable_recipient_rule;"):
            discard_repeatable_recipient_confirmation(session, admission=admission, task_id=task_id)
        _rearm_unverified_current_follows(session, admission, message_id, channel_refs)
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id=message_id,
            text=text,
            bot_peer_id=bot_peer_id,
            is_admin_bot=is_admin_bot,
            is_trusted_source=True,
            control_buttons=control_buttons,
            bound_task_id=task_id,
            bind_confirmation_source=not follow_only,
        )
        admission.failure_code = ""
        if not follow_only or not admission.source_message_id:
            admission.evidence_ref = f"attr:{evidence_kind};msg:{message_id}"
        applied.append(admission)
    session.flush()
    return applied


def is_repeatable_recipient_channel_rule(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    message_id: str,
    bot_peer_id: str,
    text: str,
    control_buttons: list[dict[str, object]],
) -> bool:
    signature = _channel_requirement_signature(text, control_buttons)
    if signature is None:
        return False
    prior_messages = session.scalars(
        select(GroupContextMessage)
        .where(
            GroupContextMessage.tenant_id == tenant_id,
            GroupContextMessage.group_id == group_id,
            GroupContextMessage.is_bot.is_(True),
            GroupContextMessage.sender_peer_id == bot_peer_id,
            GroupContextMessage.remote_message_id != message_id,
        )
        .order_by(GroupContextMessage.id.desc())
        .limit(REPEATABLE_RECIPIENT_RULE_CONTEXT_LIMIT)
    )
    return any(
        _channel_requirement_signature(message.content, list(message.control_buttons or [])) == signature
        for message in prior_messages
    )


def _channel_requirement_signature(
    text: str,
    control_buttons: list[dict[str, object]],
) -> tuple[tuple[str, ...], tuple[int, int, str]] | None:
    channel_refs = tuple(sorted(ref.casefold() for ref in parse_channel_refs(text, control_buttons)))
    if not channel_refs:
        return None
    confirmation = confirmation_button(control_buttons)
    confirmation_shape = (
        int((confirmation or {}).get("row") or 0),
        int((confirmation or {}).get("col") or 0),
        str((confirmation or {}).get("text") or "").casefold(),
    )
    return channel_refs, confirmation_shape


def _rearm_unverified_current_follows(
    session: Session,
    admission: GroupBotAdmission,
    message_id: str,
    channel_refs: tuple[str, ...],
) -> None:
    if not message_id:
        return
    follows = session.scalars(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.channel_ref.in_(channel_refs),
            GroupBotRequiredChannelFollow.status == "blocked",
            GroupBotRequiredChannelFollow.failure_code == "group_bot_control_prompt_unverified",
        )
    )
    for follow in follows:
        if follow.source_message_id == message_id:
            continue
        follow.source_message_id = message_id
        follow.action_id = ""
        follow.resolved_peer_id = ""
        follow.resolved_type = ""
        follow.status = "pending"
        follow.failure_code = ""
        follow.completed_at = None


def _global_rule_authorized(
    session: Session,
    tenant_id: int,
    group_id: int,
    bot_peer_id: str,
    is_admin_bot: bool,
) -> bool:
    if is_admin_bot:
        return True
    return any(
        active_policy(
            session,
            tenant_id=tenant_id,
            group_id=group_id,
            completion_policy=policy_type,
            trusted_bot_peer_id=bot_peer_id,
        )
        is not None
        for policy_type in SOURCE_BOUND_POLICY_TYPES
    )


def _candidate_admissions(
    session: Session,
    tenant_id: int,
    group_id: int,
    bot_peer_id: str,
) -> list[GroupBotAdmission]:
    return list(
        session.scalars(
            select(GroupBotAdmission).where(
                GroupBotAdmission.tenant_id == tenant_id,
                GroupBotAdmission.group_id == group_id,
                GroupBotAdmission.state.in_(GLOBAL_RULE_STATES),
                GroupBotAdmission.trusted_bot_peer_id.in_(("", bot_peer_id)),
            )
        )
    )


def _bound_running_task_id(session: Session, admission: GroupBotAdmission) -> str:
    task_id = session.scalar(
        select(Task.id)
        .join(TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.task_id == Task.id)
        .where(
            Task.tenant_id == admission.tenant_id,
            Task.type == "group_ai_chat",
            Task.status == "running",
            Task.deleted_at.is_(None),
            Task.type_config["target_group_id"].as_integer() == admission.group_id,
            TaskMembershipAdmissionItem.tenant_id == admission.tenant_id,
            TaskMembershipAdmissionItem.account_id == admission.account_id,
        )
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .limit(1)
    )
    return str(task_id or "")
