from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    OperationTarget,
    Task,
)

from .channel_fulfillment_takeover import ChannelTakeoverSummary


MIGRATABLE_COMMENT_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed", "success", "unknown_after_send"}
)
ACTIVE_COMMENT_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)


def migrate_comment_fulfillment(
    session: Session,
    task: Task,
    *,
    now: datetime,
) -> ChannelTakeoverSummary:
    actions = _comment_actions(session, task)
    if not actions:
        return ChannelTakeoverSummary()
    next_ordinals = _next_ordinals(session, task)
    summary = ChannelTakeoverSummary()
    for action in actions:
        migrated = _migrate_action(
            session,
            task,
            action,
            now=now,
            next_ordinals=next_ordinals,
        )
        summary = _sum_summary(summary, migrated)
    session.flush()
    return summary


def ensure_comment_action_contract(
    session: Session,
    action: Action,
    *,
    now: datetime,
) -> bool:
    payload = dict(action.payload or {})
    existing = _payload_obligation(
        session,
        _action_task(session, action),
        payload,
        lock=True,
    )
    if existing is not None:
        return _restore_existing_action_binding(session, existing, action)
    task = session.scalar(
        select(Task).where(Task.id == action.task_id).with_for_update()
    )
    if task is None or task.type != "channel_comment":
        raise ValueError(f"comment_takeover_task_missing:{action.id}")
    migrate_comment_fulfillment(session, task, now=now)
    obligation_id = str((action.payload or {}).get("comment_fulfillment_obligation_id") or "")
    if not obligation_id:
        raise ValueError(f"comment_takeover_obligation_missing:{action.id}")
    return True


def _restore_existing_action_binding(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action,
) -> bool:
    if obligation.current_action_id == action.id:
        return True
    if obligation.status == "confirmed":
        return False
    if obligation.current_action_id:
        current = session.get(Action, obligation.current_action_id)
        if current is not None and current.status not in {
            "cancelled",
            "failed",
            "skipped",
            "retryable_failed",
        }:
            return False
    obligation.current_action_id = action.id
    obligation.action_attempt_no = int(obligation.action_attempt_no or 0) + 1
    obligation.status = "pending"
    return True


def _action_task(session: Session, action: Action) -> Task:
    task = session.get(Task, action.task_id)
    if task is None:
        raise ValueError(f"comment_takeover_task_missing:{action.id}")
    return task


def _comment_actions(session: Session, task: Task) -> list[Action]:
    return list(session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.action_type == "post_comment",
            Action.status.in_(MIGRATABLE_COMMENT_STATUSES),
        )
        .order_by(Action.created_at, Action.id)
    ))


def _next_ordinals(session: Session, task: Task) -> dict[int, int]:
    obligations = session.scalars(
        select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.task_id == task.id
        )
    )
    values: dict[int, int] = {}
    for obligation in obligations:
        values[obligation.channel_message_id] = max(
            values.get(obligation.channel_message_id, 1),
            obligation.target_ordinal + 1,
        )
    return values


def _migrate_action(
    session: Session,
    task: Task,
    action: Action,
    *,
    now: datetime,
    next_ordinals: dict[int, int],
) -> ChannelTakeoverSummary:
    payload = dict(action.payload or {})
    message = _channel_message(session, task, payload)
    if message is None:
        raise ValueError(f"comment_takeover_message_missing:{action.id}")
    obligation = _resolve_obligation(
        session,
        task,
        action,
        message,
        payload,
        next_ordinals,
    )
    action.payload = _bound_payload(payload, obligation)
    if action.status == "success":
        return _confirm_historic_success(session, action, obligation, now=now)
    _bind_nonconfirmed_action(obligation, action)
    return ChannelTakeoverSummary(bound_action_count=1)


def _channel_message(
    session: Session,
    task: Task,
    payload: dict,
) -> ChannelMessage | None:
    database_id = _positive_int(payload.get("channel_message_id"))
    if database_id:
        message = session.get(ChannelMessage, database_id)
        if message and message.tenant_id == task.tenant_id:
            return message
    target_id = _positive_int(payload.get("channel_target_id"))
    telegram_id = _positive_int(payload.get("message_id"))
    if not target_id or not telegram_id:
        return None
    return session.scalar(
        select(ChannelMessage).where(
            ChannelMessage.tenant_id == task.tenant_id,
            ChannelMessage.channel_target_id == target_id,
            ChannelMessage.message_id == telegram_id,
        )
    )


def _resolve_obligation(
    session: Session,
    task: Task,
    action: Action,
    message: ChannelMessage,
    payload: dict,
    next_ordinals: dict[int, int],
) -> CommentFulfillmentObligation:
    existing = _payload_obligation(session, task, payload)
    if existing is not None:
        return existing
    revision = _positive_int(payload.get("comment_plan_revision")) or int(
        task.config_revision or 1
    )
    ordinal = _positive_int(payload.get("target_ordinal"))
    if not ordinal:
        ordinal = next_ordinals.get(message.id, 1)
        next_ordinals[message.id] = ordinal + 1
    obligation = session.scalar(
        select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.channel_message_id == message.id,
            CommentFulfillmentObligation.comment_plan_revision == revision,
            CommentFulfillmentObligation.target_ordinal == ordinal,
        )
    )
    if obligation is not None:
        return obligation
    obligation = _new_obligation(task, action, message, payload, revision, ordinal)
    session.add(obligation)
    session.flush()
    return obligation


def _payload_obligation(
    session: Session,
    task: Task,
    payload: dict,
    *,
    lock: bool = False,
) -> CommentFulfillmentObligation | None:
    obligation_id = str(payload.get("comment_fulfillment_obligation_id") or "")
    if not obligation_id:
        return None
    statement = select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.id == obligation_id
    )
    obligation = session.scalar(
        statement.with_for_update() if lock else statement
    )
    return obligation if obligation and obligation.task_id == task.id else None


def _new_obligation(
    task: Task,
    action: Action,
    message: ChannelMessage,
    payload: dict,
    revision: int,
    ordinal: int,
) -> CommentFulfillmentObligation:
    reply_id = _positive_int(payload.get("reply_to_message_id")) or None
    return CommentFulfillmentObligation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=revision,
        target_ordinal=ordinal,
        relation_kind="reply" if reply_id else "direct",
        reply_to_message_id=reply_id,
        reply_target_snapshot=_reply_snapshot(payload),
        current_action_id=action.id,
        action_attempt_no=1,
        status="pending",
    )


def _reply_snapshot(payload: dict) -> dict:
    return {
        key: payload[key]
        for key in (
            "reply_to_message_id",
            "reply_target_label",
            "reply_target_author",
            "reply_target_preview",
            "reply_target_source",
        )
        if payload.get(key) not in (None, "")
    }


def _bound_payload(
    payload: dict,
    obligation: CommentFulfillmentObligation,
) -> dict:
    return {
        **payload,
        "channel_message_id": obligation.channel_message_id,
        "comment_fulfillment_obligation_id": obligation.id,
        "comment_plan_revision": obligation.comment_plan_revision,
        "target_ordinal": obligation.target_ordinal,
        "comment_action_attempt_no": obligation.action_attempt_no,
        "content_mix_contract_id": obligation.content_mix_contract_id or "",
    }


def _confirm_historic_success(
    session: Session,
    action: Action,
    obligation: CommentFulfillmentObligation,
    *,
    now: datetime,
) -> ChannelTakeoverSummary:
    attempt = _successful_attempt(session, action.id)
    remote_id = str(attempt.remote_message_id or "") if attempt else ""
    if not remote_id:
        obligation.status = "unknown"
        obligation.current_action_id = action.id
        return ChannelTakeoverSummary(bound_action_count=1)
    peer_id = _discussion_peer(session, action, obligation)
    duplicate = _remote_fact_obligation(session, peer_id, remote_id)
    if duplicate is not None and duplicate.id != obligation.id:
        obligation.status = "replan_required"
        obligation.current_action_id = None
        return ChannelTakeoverSummary(duplicate_action_count=1)
    obligation.status = "confirmed"
    obligation.current_action_id = action.id
    obligation.telegram_discussion_peer_id = peer_id
    obligation.remote_comment_id = remote_id
    obligation.remote_confirmed_at = attempt.after_call_at or now
    return ChannelTakeoverSummary(
        bound_action_count=1,
        backfilled_fact_count=1,
    )


def _successful_attempt(
    session: Session,
    action_id: str,
) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(
            ExecutionAttempt.action_id == action_id,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
        .order_by(ExecutionAttempt.attempt_no.desc())
    )


def _discussion_peer(
    session: Session,
    action: Action,
    obligation: CommentFulfillmentObligation,
) -> str:
    payload = action.payload or {}
    peer_id = str(payload.get("channel_id") or "")
    if peer_id:
        return peer_id
    message = session.get(ChannelMessage, obligation.channel_message_id)
    target = session.get(OperationTarget, message.channel_target_id) if message else None
    if target and target.tg_peer_id:
        return str(target.tg_peer_id)
    raise ValueError(f"comment_takeover_peer_missing:{action.id}")


def _remote_fact_obligation(
    session: Session,
    peer_id: str,
    remote_id: str,
) -> CommentFulfillmentObligation | None:
    return session.scalar(
        select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.telegram_discussion_peer_id == peer_id,
            CommentFulfillmentObligation.remote_comment_id == remote_id,
        )
    )


def _bind_nonconfirmed_action(
    obligation: CommentFulfillmentObligation,
    action: Action,
) -> None:
    obligation.current_action_id = action.id
    obligation.action_attempt_no = max(1, obligation.action_attempt_no)
    obligation.status = (
        "unknown" if action.status == "unknown_after_send" else "pending"
    )


def _positive_int(value: object) -> int:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 0


def _sum_summary(
    left: ChannelTakeoverSummary,
    right: ChannelTakeoverSummary,
) -> ChannelTakeoverSummary:
    return ChannelTakeoverSummary(
        bound_action_count=left.bound_action_count + right.bound_action_count,
        backfilled_fact_count=left.backfilled_fact_count + right.backfilled_fact_count,
        duplicate_action_count=left.duplicate_action_count + right.duplicate_action_count,
    )


__all__ = ["ensure_comment_action_contract", "migrate_comment_fulfillment"]
