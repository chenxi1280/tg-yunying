from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, CommentFulfillmentObligation, Task

from ..channel_comment_plan_contract import ensure_comment_plan_contract
from ..comment_fulfillment import freeze_comment_obligations
from .channel_comment_schedule import reply_minimum_for_mode
from .channel_comment_targets import valid_reply_targets
from .common import stats_inc


@dataclass(frozen=True)
class CommentPlanSlot:
    message: ChannelMessage
    reply_target: dict | None
    slot_index: int
    obligation: CommentFulfillmentObligation
    grounding_assignment: object | None = None
    source_revision_id: str = ""


def build_grounding_comment_plan_slots(
    session: Session,
    task: Task,
    context: Any,
    *,
    input_allowed: Callable,
    target_builder: Callable,
) -> list[CommentPlanSlot] | None:
    reply_targets = _grounding_reply_targets(session, task, context)
    if reply_targets is None:
        return None
    slots: list[CommentPlanSlot] = []
    for message in context.messages:
        if not input_allowed(task, context, message):
            continue
        plan = _safe_plan(session, task, message, accounts=context.accounts)
        if plan is None:
            continue
        existing = _plan_obligations(session, plan.contract.id)
        obligations = existing or _freeze_grounding_obligations(
            session,
            task=task,
            context=context,
            message=message,
            plan=plan,
            reply_targets=reply_targets,
            target_builder=target_builder,
        )
        slots.extend(_open_plan_slots(
            message,
            obligations,
            plan.assignment_by_ordinal,
            source_revision_id=plan.contract.source_revision_id,
        ))
    return slots


def _safe_plan(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    accounts: list,
):
    try:
        return ensure_comment_plan_contract(
            session, task, message, accounts=accounts,
        )
    except ValueError as exc:
        stats_inc(task, str(exc))
        return None


def _grounding_reply_targets(
    session: Session,
    task: Task,
    context: Any,
) -> list[dict] | None:
    requested = [
        int(item) for item in context.config.get("reply_to_message_ids") or []
        if int(item or 0) > 0
    ]
    targets = valid_reply_targets(
        session, task, context.channel.id, context.messages, requested,
    )
    mode = str(context.config.get("comment_mode") or "comment")
    if mode in {"reply", "mixed"} and requested and not targets:
        task.last_error = "回复对象不属于当前频道消息，请先采集评论后重新选择"
        return None
    return targets


def _plan_obligations(
    session: Session,
    plan_contract_id: str,
) -> list[CommentFulfillmentObligation]:
    return list(session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id == plan_contract_id,
    ).order_by(CommentFulfillmentObligation.target_ordinal)))


def _freeze_grounding_obligations(
    session: Session,
    *,
    task: Task,
    context: Any,
    message: ChannelMessage,
    plan: Any,
    reply_targets: list[dict],
    target_builder: Callable,
) -> list[CommentFulfillmentObligation]:
    total = int(plan.contract.required_distinct_account_count)
    targets = target_builder(
        session, task, context, message, total, reply_targets,
    )
    if targets is None:
        return []
    first_fallback = int(plan.contract.grounding_required_count) + 1
    return freeze_comment_obligations(
        session, task, message, targets,
        rule_version=context.rule_version,
        reply_min_required=reply_minimum_for_mode(
            context.config.get("comment_mode") or "comment", total, context.config,
        ),
        plan_contract_id=plan.contract.id,
        revision_override=plan.contract.comment_plan_revision,
        account_by_ordinal=plan.account_by_ordinal,
        grounding_assignment_by_ordinal=plan.assignment_by_ordinal,
        planned_fallback_ordinals=set(range(first_fallback, total + 1)),
    )


def _open_plan_slots(
    message: ChannelMessage,
    obligations: list[CommentFulfillmentObligation],
    assignments: dict[int, object],
    *,
    source_revision_id: str,
) -> list[CommentPlanSlot]:
    return [
        CommentPlanSlot(
            message,
            item.reply_target_snapshot if item.relation_kind == "reply" else None,
            item.target_ordinal - 1,
            item,
            assignments.get(int(item.target_ordinal)),
            source_revision_id,
        )
        for item in obligations
        if item.status in {"open", "replan_required"}
        and item.current_action_id is None
    ]


__all__ = ["CommentPlanSlot", "build_grounding_comment_plan_slots"]
