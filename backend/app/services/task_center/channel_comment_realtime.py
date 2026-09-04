from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, CommentFulfillmentObligation, Task
from app.timezone import as_beijing

from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_capacity import release_comment_capacity
from .channel_comment_source import comment_source_window
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .source_pacing_release import release_source_pacing_admissions_before_gateway
from .target_lifecycle import action_has_gateway_started


UNIFIED_ENGAGEMENT_CONTRACT = "unified_engagement_v1"
MAX_RESPONSE_RATIO_BPS = 6500
QUESTION_DELAY_SECONDS = (30, 180)
ACTIVE_DELAY_SECONDS = (60, 300)
ORDINARY_DELAY_SECONDS = (180, 900)
QUESTION_MARKERS = ("?", "？", "怎么", "如何", "多少", "能否", "吗", "么")
PREEMPTED_REASON = "discussion_response_preempted_direct_before_gateway"


def promote_human_comment_response(
    session: Session,
    task: Task,
    message,
    *,
    target: dict,
    now_value: datetime,
) -> bool:
    if not _eligible_task(task) or _target_already_owned(
        session, task, message_id=message.id, target=target,
    ):
        return False
    obligations = _mutable_direct_obligations(session, task, message.id)
    if not obligations or not _response_capacity_available(session, task, message.id):
        return False
    planned_at = _planned_response_at(message, target, now_value=now_value)
    if not _inside_source_window(task, message, planned_at):
        return False
    obligation = next(
        (row for row in obligations if _release_bound_action(session, row)),
        None,
    )
    if obligation is None:
        return False
    _bind_response_intent(obligation, target, planned_at=planned_at)
    return True


def refresh_human_comment_response(
    session: Session,
    task: Task,
    message,
    *,
    target: dict,
    now_value: datetime,
) -> bool:
    planned_at = _planned_response_at(message, target, now_value=now_value)
    if not _inside_source_window(task, message, planned_at):
        return False
    obligation = _reply_obligation(
        session, task,
        message_id=message.id,
        remote_message_id=int(target.get("message_id") or 0),
    )
    if obligation is None or not _release_bound_action(session, obligation):
        return False
    _bind_response_intent(obligation, target, planned_at=planned_at)
    return True


def release_deleted_human_response(
    session: Session,
    task: Task,
    message,
    *,
    remote_message_id: int,
) -> bool:
    obligation = _reply_obligation(
        session, task,
        message_id=message.id,
        remote_message_id=remote_message_id,
    )
    if obligation is None or not _release_bound_action(session, obligation):
        return False
    obligation.relation_kind = "direct"
    obligation.reply_to_message_id = None
    obligation.reply_target_snapshot = {}
    obligation.rpc_mode = (
        "channel_comment_to" if obligation.grounding_enrollment_id else obligation.rpc_mode
    )
    obligation.pacing_due_at = None
    obligation.release_not_before_at = None
    obligation.status = "replan_required"
    return True


def _eligible_task(task: Task) -> bool:
    return bool(
        task.type == "channel_comment"
        and task.status in {"pending", "running"}
        and (task.type_config or {}).get("engagement_contract_version")
        == UNIFIED_ENGAGEMENT_CONTRACT
    )


def _mutable_direct_obligations(
    session: Session,
    task: Task,
    message_id: int,
) -> list[CommentFulfillmentObligation]:
    statement = select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.task_id == task.id,
        CommentFulfillmentObligation.channel_message_id == message_id,
        CommentFulfillmentObligation.relation_kind == "direct",
        CommentFulfillmentObligation.fallback_intent_kind != "planned",
        CommentFulfillmentObligation.status.in_(("open", "replan_required", "pending")),
    ).order_by(
        CommentFulfillmentObligation.pacing_due_at.asc().nulls_last(),
        CommentFulfillmentObligation.target_ordinal.asc(),
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return list(session.scalars(statement))


def _reply_obligation(
    session: Session,
    task: Task,
    *,
    message_id: int,
    remote_message_id: int,
) -> CommentFulfillmentObligation | None:
    statement = select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.task_id == task.id,
        CommentFulfillmentObligation.channel_message_id == message_id,
        CommentFulfillmentObligation.reply_to_message_id == remote_message_id,
        CommentFulfillmentObligation.status.in_(("open", "replan_required", "pending")),
    ).limit(1)
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _release_bound_action(
    session: Session,
    obligation: CommentFulfillmentObligation,
) -> bool:
    if not obligation.current_action_id:
        return True
    action = session.get(Action, obligation.current_action_id)
    if action is None or action.status not in {"pending", "retryable_failed"}:
        return False
    if action_has_gateway_started(session, action):
        return False
    payload = PostCommentPayload.model_validate(action.payload or {})
    invalidate_comment_generation_jobs(
        session,
        action,
        payload,
        reason=PREEMPTED_REASON,
    )
    release_action_pacing_reservation_before_gateway(session, action)
    release_source_pacing_admissions_before_gateway(session, action)
    release_comment_capacity(session, obligation.id)
    action.status = "cancelled"
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {**dict(action.result or {}), "error_code": PREEMPTED_REASON}
    obligation.current_action_id = None
    return True


def _response_capacity_available(session: Session, task: Task, message_id: int) -> bool:
    total, replies = session.execute(select(
        func.count(CommentFulfillmentObligation.id),
        func.count(CommentFulfillmentObligation.id).filter(
            CommentFulfillmentObligation.relation_kind == "reply"
        ),
    ).where(
        CommentFulfillmentObligation.task_id == task.id,
        CommentFulfillmentObligation.channel_message_id == message_id,
    )).one()
    response_limit = max(1, int(int(total or 0) * MAX_RESPONSE_RATIO_BPS / 10000))
    return int(replies or 0) < response_limit


def _target_already_owned(
    session: Session,
    task: Task,
    *,
    message_id: int,
    target: dict,
) -> bool:
    remote_id = int(target.get("message_id") or 0)
    return bool(remote_id and session.scalar(select(CommentFulfillmentObligation.id).where(
        CommentFulfillmentObligation.task_id == task.id,
        CommentFulfillmentObligation.channel_message_id == message_id,
        CommentFulfillmentObligation.reply_to_message_id == remote_id,
        CommentFulfillmentObligation.status.notin_(("closed_expired", "terminated")),
    ).limit(1)))


def _planned_response_at(message, target: dict, *, now_value: datetime) -> datetime:
    content = str(target.get("preview") or "")
    if any(marker in content for marker in QUESTION_MARKERS):
        bounds = QUESTION_DELAY_SECONDS
    elif int(target.get("reply_count") or 0) > 0:
        bounds = ACTIVE_DELAY_SECONDS
    else:
        bounds = ORDINARY_DELAY_SECONDS
    seed = f"{message.id}:{target.get('message_id')}:{target.get('content_hash')}"
    sample = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)
    delay = bounds[0] + sample % (bounds[1] - bounds[0] + 1)
    return now_value + timedelta(seconds=delay)


def _inside_source_window(task: Task, message, planned_at: datetime) -> bool:
    window = comment_source_window(task, message)
    if window is None:
        return False
    _start, deadline = window
    left = as_beijing(planned_at)
    right = as_beijing(deadline)
    return left <= right


def _bind_response_intent(
    obligation: CommentFulfillmentObligation,
    target: dict,
    *,
    planned_at: datetime,
) -> None:
    obligation.relation_kind = "reply"
    obligation.reply_to_message_id = int(target["message_id"])
    obligation.reply_target_snapshot = dict(target)
    obligation.rpc_mode = "discussion_reply_to" if obligation.grounding_enrollment_id else obligation.rpc_mode
    obligation.pacing_due_at = planned_at
    obligation.release_not_before_at = planned_at
    obligation.status = "replan_required"


__all__ = [
    "promote_human_comment_response",
    "refresh_human_comment_response",
    "release_deleted_human_response",
]
