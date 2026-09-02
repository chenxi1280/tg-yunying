from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingEnrollment,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionThreadBinding,
    ChannelMessageSourceRevision,
    DiscussionMembershipFact,
    Task,
)

from .channel_comment_discussion_contracts import current_membership_fact, membership_ready
from .channel_comment_discussion_freshness import group_binding_fresh, thread_binding_fresh
from .channel_comment_grounding_enrollment import active_grounding_enrollment


@dataclass(frozen=True)
class DiscussionPlanIdentity:
    enrollment: ChannelCommentGroundingEnrollment
    group_binding: ChannelDiscussionGroupBinding
    thread_binding: ChannelDiscussionThreadBinding
    membership_by_account: dict[int, DiscussionMembershipFact]
    admission_action_by_account: dict[int, Action]
    admission_candidate_ids: frozenset[int] = frozenset()


def resolve_discussion_plan_identity(
    session: Session,
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    accounts: list,
    now_value: datetime | None = None,
) -> DiscussionPlanIdentity:
    observed_at = now_value or datetime.now(timezone.utc)
    enrollment = active_grounding_enrollment(session, task)
    if enrollment is None:
        _block(task, "channel_comment_grounding_not_enrolled")
    binding = session.get(ChannelDiscussionGroupBinding, enrollment.group_binding_id)
    _require_plan_binding(
        session, task, source,
        enrollment=enrollment, binding=binding, now_value=observed_at,
    )
    thread = session.get(ChannelDiscussionThreadBinding, source.discussion_thread_binding_id)
    _require_plan_thread(
        session, task, source,
        binding=binding, thread=thread, now_value=observed_at,
    )
    memberships = _ready_memberships(
        session, task, binding=binding, accounts=accounts, now_value=observed_at,
    )
    from .channel_comment_discussion_admission import discussion_admission_candidate_ids

    candidates = discussion_admission_candidate_ids(
        session, task, binding,
        accounts=accounts,
        now_value=observed_at,
    )
    return DiscussionPlanIdentity(
        enrollment, binding, thread, memberships, {}, candidates,
    )


def _require_plan_binding(
    session: Session,
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    enrollment: ChannelCommentGroundingEnrollment,
    binding: ChannelDiscussionGroupBinding | None,
    now_value: datetime,
) -> None:
    if _wall(source.source_published_at) < _wall(enrollment.enabled_at):
        _block(task, "source_before_grounding_enrollment")
    if binding is None or not binding.is_current or binding.binding_status != "active":
        _block(task, "discussion_binding_blocked")
    if not group_binding_fresh(session, binding, now_value):
        _block(task, "discussion_binding_stale")
    expected = (binding.id, binding.binding_revision, binding.identity_hash)
    actual = (
        source.discussion_group_binding_id,
        source.discussion_group_binding_revision,
        source.discussion_group_identity_hash,
    )
    if expected != actual or enrollment.group_binding_identity_hash != binding.identity_hash:
        _block(task, "discussion_binding_identity_mismatch")


def _require_plan_thread(
    session: Session,
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    binding: ChannelDiscussionGroupBinding,
    thread: ChannelDiscussionThreadBinding | None,
    now_value: datetime,
) -> None:
    if thread is None or not thread.is_current or thread.group_binding_id != binding.id:
        _block(task, "discussion_thread_mapping_blocked")
    expected = (thread.id, thread.thread_revision, thread.identity_hash)
    actual = (
        source.discussion_thread_binding_id,
        source.discussion_thread_revision,
        source.discussion_thread_identity_hash,
    )
    if expected != actual or thread.discussion_peer_id != binding.discussion_peer_id:
        _block(task, "discussion_thread_identity_mismatch")
    if not thread_binding_fresh(session, thread, now_value):
        _block(task, "discussion_thread_mapping_stale")


def _ready_memberships(
    session: Session,
    task: Task,
    *,
    binding: ChannelDiscussionGroupBinding,
    accounts: list,
    now_value: datetime,
) -> dict[int, DiscussionMembershipFact]:
    facts: dict[int, DiscussionMembershipFact] = {}
    for account in accounts:
        fact = current_membership_fact(
            session,
            tenant_id=task.tenant_id,
            account_id=int(account.id),
            discussion_peer_id=str(binding.discussion_peer_id),
            group_binding_id=binding.id,
        )
        if membership_ready(fact, now_value):
            facts[int(account.id)] = fact
    return facts


def discussion_send_blocker(
    session: Session,
    action: Action,
    payload,
    *,
    now_value: datetime | None = None,
) -> str:
    if not payload.grounding_enrollment_id:
        return ""
    observed_at = now_value or datetime.now(timezone.utc)
    task = session.get(Task, action.task_id)
    if task is None or task.config_revision != payload.task_config_revision:
        return "channel_comment_task_config_drift"
    if task.task_lifecycle_epoch != payload.task_lifecycle_epoch:
        return "channel_comment_task_epoch_drift"
    blocker = _send_contract_blocker(
        session, action, payload=payload, now_value=observed_at,
    )
    return blocker or _rpc_identity_blocker(payload)


def _send_contract_blocker(
    session: Session,
    action: Action,
    *,
    payload,
    now_value: datetime,
) -> str:
    enrollment = session.get(ChannelCommentGroundingEnrollment, payload.grounding_enrollment_id)
    if enrollment is None or enrollment.enrollment_state != "active":
        return "channel_comment_grounding_enrollment_stale"
    binding = session.get(ChannelDiscussionGroupBinding, payload.discussion_group_binding_id)
    if binding is None or not binding.is_current:
        return "discussion_binding_changed_pre_gateway"
    if (binding.binding_revision, binding.identity_hash) != (
        payload.discussion_group_binding_revision, payload.discussion_group_identity_hash,
    ):
        return "discussion_binding_identity_mismatch"
    thread = session.get(ChannelDiscussionThreadBinding, payload.discussion_thread_binding_id)
    if thread is None or not thread.is_current:
        return "discussion_thread_changed_pre_gateway"
    if (thread.thread_revision, thread.identity_hash) != (
        payload.discussion_thread_revision, payload.discussion_thread_identity_hash,
    ):
        return "discussion_thread_identity_mismatch"
    fact = session.get(DiscussionMembershipFact, payload.membership_fact_id)
    if fact is None or fact.account_id != action.account_id or fact.group_binding_id != binding.id:
        return "discussion_membership_identity_mismatch"
    if not membership_ready(fact, now_value):
        return "discussion_membership_not_ready"
    return ""


def _rpc_identity_blocker(payload) -> str:
    if payload.rpc_mode == "channel_comment_to":
        if payload.reply_to_message_id or payload.actual_target_peer != payload.channel_id:
            return "channel_comment_rpc_identity_conflict"
        return ""
    if payload.rpc_mode != "discussion_reply_to":
        return "channel_comment_rpc_mode_invalid"
    if not payload.reply_to_message_id or payload.actual_target_peer != payload.discussion_peer_id:
        return "channel_comment_reply_identity_mismatch"
    if not payload.thread_root_message_id:
        return "discussion_thread_root_missing"
    return ""


def discussion_membership_counts(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    account_ids: list[int],
    now_value: datetime | None = None,
) -> dict[str, int]:
    observed_at = now_value or datetime.now(timezone.utc)
    ready = forbidden = unknown = admission = 0
    for account_id in account_ids:
        fact = current_membership_fact(
            session,
            tenant_id=task.tenant_id,
            account_id=account_id,
            discussion_peer_id=str(binding.discussion_peer_id),
            group_binding_id=binding.id,
        )
        if membership_ready(fact, observed_at):
            ready += 1
        elif fact is None or fact.membership_status == "not_participant":
            admission += 1
        elif fact.membership_status == "unknown":
            unknown += 1
        elif fact is not None and (fact.membership_status in {"restricted", "banned", "inaccessible"} or not fact.can_send):
            forbidden += 1
        else:
            unknown += 1
    return {
        "discussion_membership_ready_count": ready,
        "discussion_admission_required_count": admission,
        "discussion_forbidden_count": forbidden,
        "discussion_membership_unknown_count": unknown,
    }


def _block(task: Task, code: str):
    task.last_error = code
    raise ValueError(code)


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "DiscussionPlanIdentity",
    "discussion_membership_counts",
    "discussion_send_blocker",
    "resolve_discussion_plan_identity",
]
