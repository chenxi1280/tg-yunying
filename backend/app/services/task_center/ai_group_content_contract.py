from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    OperationTarget,
    Task,
    TaskDayLedger,
)

from .payloads import SendMessagePayload


CONTENT_CONTRACT_REVISION = "ai_group_content_v1_2"


def validate_content_intent_contract(
    session: Session,
    payload: SendMessagePayload,
    *,
    action: Action | None,
    capacity_projection: Callable,
    remote_boundary: bool = False,
) -> None:
    if not payload.allocation_plan_id:
        return
    plan = session.get(AiGroupContentAllocationPlan, payload.allocation_plan_id)
    intent = session.get(AiGroupContentIntent, payload.content_intent_id)
    if plan is None or intent is None or intent.allocation_plan_id != plan.id:
        raise ValueError("topic_capacity_contract_invalid")
    bound_action = action or _action_for_slot(session, payload.primary_quantity_slot_id)
    _validate_scope(session, plan, intent, payload, bound_action)
    _validate_topic(plan, intent, payload)
    if intent.topic_mode == "configured_topic":
        _validate_topic_capacity(
            plan,
            capacity_projection(session, plan.id),
            remote_boundary=remote_boundary,
        )
    _validate_vocabulary(plan, intent, payload)


def _action_for_slot(session: Session, quantity_slot_id: str) -> Action | None:
    if not quantity_slot_id:
        return None
    return session.scalar(
        select(Action)
        .where(Action.primary_quantity_slot_id == quantity_slot_id)
        .order_by(Action.created_at.desc(), Action.id.desc())
        .limit(1)
    )


def _validate_scope(
    session: Session,
    plan: AiGroupContentAllocationPlan,
    intent: AiGroupContentIntent,
    payload: SendMessagePayload,
    action: Action | None,
) -> None:
    task = session.get(Task, plan.task_id)
    ledger = session.get(TaskDayLedger, plan.task_day_ledger_id)
    target = session.get(OperationTarget, plan.target_operation_target_id)
    if task is None or ledger is None or target is None:
        raise ValueError("content_allocation_scope_missing")
    expected_ratio_scope = (
        f"task:{plan.task_id}:day:{plan.task_day.isoformat()}:"
        f"target:{plan.target_operation_target_id}"
    )
    values_match = (
        intent.task_id == plan.task_id
        and intent.tenant_id == plan.tenant_id == task.tenant_id
        and intent.primary_quantity_slot_id == payload.primary_quantity_slot_id
        and payload.target_operation_target_id == plan.target_operation_target_id
        and payload.content_task_day == plan.task_day.isoformat()
        and ledger.obligation_local_date == plan.task_day
        and payload.route_family == plan.route_family
        and payload.surface_scope_key == plan.surface_scope_key
        and payload.topic_ratio_scope_key == expected_ratio_scope
        and payload.content_contract_revision == CONTENT_CONTRACT_REVISION
        and payload.normal_text_ordinal == intent.normal_text_ordinal
        and payload.topic_budget_eligible == bool(intent.topic_budget_eligible)
        and payload.relation_kind == intent.relation_kind
        and payload.act_type == intent.act_type
        and payload.content_intent_stance == intent.stance
    )
    if not values_match:
        raise ValueError("topic_contract_revision_drift")
    _validate_revisions(task, target, intent, payload, action)


def _validate_revisions(
    task: Task,
    target: OperationTarget,
    intent: AiGroupContentIntent,
    payload: SendMessagePayload,
    action: Action | None,
) -> None:
    values_match = (
        payload.content_intent_config_revision == intent.config_revision
        and payload.content_intent_config_snapshot_hash == intent.config_snapshot_hash
        and payload.content_intent_task_lifecycle_epoch == intent.task_lifecycle_epoch
        and payload.content_intent_target_reference_revision
        == intent.target_reference_revision
        and int(target.reference_revision or 1) == intent.target_reference_revision
    )
    if action is not None:
        values_match = values_match and (
            action.task_id == task.id
            and int(action.task_lifecycle_epoch or 1) == intent.task_lifecycle_epoch
        )
    if not values_match:
        raise ValueError("topic_contract_revision_drift")


def _validate_topic(
    plan: AiGroupContentAllocationPlan,
    intent: AiGroupContentIntent,
    payload: SendMessagePayload,
) -> None:
    if (
        payload.topic_rate_bps != plan.topic_rate_bps
        or payload.topic_mode != intent.topic_mode
    ):
        raise ValueError("topic_contract_revision_drift")
    if dict(payload.topic_direction) != dict(intent.topic_direction_snapshot):
        raise ValueError("topic_contract_revision_drift")
    if dict(payload.teacher_target) != dict(intent.teacher_target_snapshot):
        raise ValueError("teacher_contract_revision_drift")
    if intent.topic_mode == "configured_topic":
        if not intent.topic_capacity_reservation_id:
            raise ValueError("topic_capacity_contract_invalid")
        if (
            payload.topic_capacity_reservation_id
            != intent.topic_capacity_reservation_id
        ):
            raise ValueError("topic_capacity_contract_invalid")
        return
    if payload.topic_capacity_reservation_id or payload.topic_direction:
        raise ValueError("topic_capacity_contract_invalid")


def _validate_topic_capacity(
    plan: AiGroupContentAllocationPlan,
    projection,
    *,
    remote_boundary: bool,
) -> None:
    if remote_boundary:
        numerator = projection.confirmed_topic_count + projection.unknown_topic_count + 1
        denominator = projection.confirmed_normal_count + projection.unknown_topic_count + 1
    else:
        numerator = (
            projection.confirmed_topic_count
            + projection.unknown_topic_count
            + projection.active_topic_reservations
        )
        denominator = (
            projection.confirmed_normal_count
            + projection.unknown_topic_count
            + projection.active_normal_reservations
        )
    if not denominator or numerator * 10000 > denominator * plan.topic_rate_bps:
        raise ValueError("topic_capacity_contract_invalid")


def _validate_vocabulary(
    plan: AiGroupContentAllocationPlan,
    intent: AiGroupContentIntent,
    payload: SendMessagePayload,
) -> None:
    if payload.daily_vocabulary_theme_id != plan.daily_vocabulary_theme_id:
        raise ValueError("daily_theme_contract_invalid")
    if payload.daily_vocabulary_theme_version != plan.daily_vocabulary_theme_version:
        raise ValueError("daily_theme_contract_invalid")
    if (
        payload.daily_vocabulary_theme_effective_state
        != intent.daily_vocabulary_theme_effective_state
    ):
        raise ValueError("daily_theme_contract_invalid")
    if payload.vocabulary_catalog_version != intent.vocabulary_catalog_version:
        raise ValueError("vocabulary_reservation_contract_invalid")
    if list(payload.vocabulary_sample_ids) != list(intent.vocabulary_sample_ids):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if list(payload.vocabulary_surface_terms) != list(intent.vocabulary_surface_terms):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if list(payload.vocabulary_normalized_term_ids) != list(
        intent.vocabulary_normalized_term_ids
    ):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if bool(payload.vocabulary_sample_ids) != bool(payload.vocabulary_reservation_id):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if bool(intent.vocabulary_sample_ids) != bool(intent.vocabulary_reservation_id):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if payload.vocabulary_sample_ids and (
        not intent.vocabulary_reservation_id
        or payload.vocabulary_reservation_id != intent.vocabulary_reservation_id
    ):
        raise ValueError("vocabulary_reservation_contract_invalid")
    if payload.vocabulary_candidate_count != intent.vocabulary_candidate_count:
        raise ValueError("vocabulary_reservation_contract_invalid")


__all__ = ["CONTENT_CONTRACT_REVISION", "validate_content_intent_contract"]
