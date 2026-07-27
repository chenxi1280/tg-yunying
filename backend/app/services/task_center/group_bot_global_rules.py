"""Apply an audited, recipient-free group-bot rule to scoped admissions only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupBotAdmission, GroupBotRequiredChannelFollow, Task, TaskMembershipAdmissionItem

from .group_bot_admission import (
    SOURCE_BOUND_POLICY_TYPES,
    active_policy,
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
) -> list[GroupBotAdmission]:
    """Plan exact admission actions for a trusted, recipient-free group-wide prompt."""
    if not _global_rule_authorized(session, tenant_id, group_id, bot_peer_id, is_admin_bot):
        return []
    channel_refs = tuple(parse_channel_refs(text, control_buttons))
    if not is_group_bot_control_prompt(text, control_buttons) or not channel_refs:
        return []
    applied: list[GroupBotAdmission] = []
    for admission in _candidate_admissions(session, tenant_id, group_id, bot_peer_id):
        task_id = _bound_running_task_id(session, admission)
        if not task_id:
            continue
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
        )
        admission.failure_code = ""
        admission.evidence_ref = f"attr:trusted_global_rule;msg:{message_id}"
        applied.append(admission)
    session.flush()
    return applied


def _rearm_unverified_current_follows(
    session: Session,
    admission: GroupBotAdmission,
    message_id: str,
    channel_refs: tuple[str, ...],
) -> None:
    if admission.state != "group_bot_rule_unattributed" or not message_id:
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
