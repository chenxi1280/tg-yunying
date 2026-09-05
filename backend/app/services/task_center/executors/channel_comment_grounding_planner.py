from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, CommentFulfillmentObligation, Task

from ..channel_comment_plan_contract import ensure_comment_plan_contract
from ..channel_comment_quality_target import quality_target_projection, target_component_for_ordinal
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
    quality_target_revision_id: str = ""
    grounding_snapshot_id: str = ""
    comment_grounding_revision: int = 0
    grounding_evidence_hash: str = ""


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
        plan = _safe_plan(
            session,
            task,
            message,
            accounts=context.policy_accounts,
            ledger=context.ledger,
            participation_plan=context.participation_plan,
            admission_snapshot=context.admission_snapshot,
        )
        if plan is None:
            continue
        existing = _plan_obligations(session, plan.contract.id)
        if existing and not _existing_obligations_ready(task, plan.quality_target, existing):
            continue
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
            quality_target=plan.quality_target,
            memberships=plan.discussion_identity.membership_by_account,
        ))
    return slots


def _safe_plan(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    accounts: list,
    ledger,
    participation_plan,
    admission_snapshot,
):
    try:
        return ensure_comment_plan_contract(
            session,
            task,
            message,
            accounts=accounts,
            ledger=ledger,
            participation_plan=participation_plan,
            admission_snapshot=admission_snapshot,
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
        session, task,
        channel_target_id=context.channel.id,
        messages=context.messages,
        requested_ids=requested,
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
    selected_account_ids = {
        int(account_id)
        for account_id in plan.account_by_ordinal.values()
    }
    ready_account_ids = set(plan.discussion_identity.membership_by_account)
    if (not plan.discussion_identity.freeze_pending_memberships
            and not selected_account_ids.issubset(ready_account_ids)):
        task.last_error = "等待讨论组账号准入完成后物化评论义务"
        return []
    total = int(plan.contract.required_distinct_account_count)
    targets = target_builder(
        session, task, context=context, message=message,
        quantity=total, requested_targets=reply_targets,
    )
    if targets is None:
        return []
    fallback_ordinals = _planned_fallback_ordinals(plan.quality_target)
    if not _fallback_contract_ready(
        task, plan.quality_target,
        targets=targets, fallback_ordinals=fallback_ordinals,
    ):
        return []
    return freeze_comment_obligations(
        session, task, message, reply_targets=targets,
        rule_version=context.rule_version,
        reply_min_required=reply_minimum_for_mode(
            context.config.get("comment_mode") or "comment", total, context.config,
        ),
        plan_contract_id=plan.contract.id,
        revision_override=plan.contract.comment_plan_revision,
        account_by_ordinal=plan.account_by_ordinal,
        grounding_assignment_by_ordinal=plan.assignment_by_ordinal,
        planned_fallback_ordinals=fallback_ordinals,
        discussion_identity=plan.discussion_identity,
    )


def _planned_fallback_ordinals(quality_target: object) -> set[int]:
    return {
        int(value)
        for component in quality_target.component_targets_json
        for value in component.get("planned_fallback_ordinal_ids", [])
    }


def _fallback_contract_ready(
    task: Task,
    quality_target: object,
    *,
    targets: list[dict | None],
    fallback_ordinals: set[int],
) -> bool:
    reply_fallback = any(
        targets[ordinal - 1] is not None for ordinal in fallback_ordinals
    )
    return _quality_contract_ready(task, quality_target, reply_fallback=reply_fallback)


def _existing_obligations_ready(
    task: Task,
    quality_target: object,
    obligations: list[CommentFulfillmentObligation],
) -> bool:
    fallback_ordinals = _planned_fallback_ordinals(quality_target)
    reply_fallback = any(
        int(row.target_ordinal) in fallback_ordinals and row.relation_kind == "reply"
        for row in obligations
    )
    return _quality_contract_ready(task, quality_target, reply_fallback=reply_fallback)


def _quality_contract_ready(
    task: Task,
    quality_target: object,
    *,
    reply_fallback: bool,
) -> bool:
    projection = quality_target_projection(quality_target)
    if projection["fallback_business_state"] != "within_cap":
        task.last_error = "channel_comment_planned_fallback_cap_exceeded"
        stats_inc(task, "planned_fallback_cap_exceeded_count")
        return False
    if reply_fallback:
        task.last_error = "channel_comment_reply_fallback_forbidden"
        stats_inc(task, "reply_fallback_forbidden_count")
        return False
    return True


def _open_plan_slots(
    message: ChannelMessage,
    obligations: list[CommentFulfillmentObligation],
    assignments: dict[int, object],
    *,
    quality_target: object,
    memberships: dict,
) -> list[CommentPlanSlot]:
    slots = []
    for item in obligations:
        if item.status not in {"open", "replan_required"} or item.current_action_id:
            continue
        membership = memberships.get(item.account_id)
        if membership is None:
            continue
        item.membership_fact_id = membership.id
        ordinal = int(item.target_ordinal)
        assignment = assignments.get(ordinal)
        component = target_component_for_ordinal(quality_target, ordinal)
        slots.append(CommentPlanSlot(
            message=message,
            reply_target=(
                item.reply_target_snapshot if item.relation_kind == "reply" else None
            ),
            slot_index=ordinal - 1,
            obligation=item,
            grounding_assignment=assignment,
            source_revision_id=str(
                getattr(assignment, "source_revision_id", "")
                or component["source_revision_id"]
            ),
            quality_target_revision_id=str(
                getattr(assignment, "quality_target_revision_id", "")
                or quality_target.id
            ),
            grounding_snapshot_id=str(
                getattr(assignment, "grounding_snapshot_id", "")
                or component.get("grounding_snapshot_id", "")
            ),
            comment_grounding_revision=int(
                getattr(assignment, "comment_grounding_revision", 0)
                or component.get("comment_grounding_revision", 0)
            ),
            grounding_evidence_hash=str(
                getattr(assignment, "evidence_hash", "")
                or component.get("source_content_hash", "")
            ),
        ))
    return slots


__all__ = ["CommentPlanSlot", "build_grounding_comment_plan_slots"]
