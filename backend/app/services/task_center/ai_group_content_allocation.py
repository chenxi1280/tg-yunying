from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyTarget,
    TgGroup,
)

from .ai_group_daily_theme import DAILY_THEME_VERSION, get_daily_vocabulary_theme
from .ai_group_topic_allocation import (
    decide_topic_mode,
    normalize_topic_participation_rate,
)
from .ai_group_topic_policy import effective_topic_rate
from .continuity_config import content_policy_revision
from .payloads import SendMessagePayload
from .ai_group_content_intent_support import (
    assert_generic_warmup_question_capacity,
    choose_weighted_topic_direction,
    constrained_act_type,
    sample_vocabulary_for_intent,
    sampled_vocabulary_values,
    vocabulary_evidence_text,
    with_content_intent,
)
from .ai_group_content_intent_factory import new_intent_record
from .ai_group_content_history import (
    ContentHistoryMessage,
    active_history_message,
    recent_plan_histories,
)
from .ai_group_content_projection import (
    TopicCapacityProjection,
    topic_capacity_projection,
)
from .ai_pacing import AiPacingAssignment


RECENT_VOCABULARY_WINDOW = 100
ADULT_ROUTES = frozenset(
    {
        "adult_visual",
        "adult_product",
        "adult_service_inquiry",
        "adult_service_sensory",
        "adult_service",
    }
)


def freeze_content_intents(
    session: Session,
    task: Task,
    *,
    daily_group_target_id: str,
    target_operation_target_id: int,
    canonical_group_id: int,
    assignments: list[AiPacingAssignment],
    quality_items: list[dict],
    config_revision: int,
    is_generic_warmup: bool,
) -> list[dict]:
    if not assignments:
        return quality_items
    _lock_group_surface(session, canonical_group_id)
    _lock_daily_target(session, daily_group_target_id)
    ledger = _task_day_ledger(session, assignments[0])
    plan = _locked_or_new_plan(
        session,
        task,
        ledger=ledger,
        target_operation_target_id=target_operation_target_id,
        canonical_group_id=canonical_group_id,
        config_revision=content_policy_revision(
            task.type_config or {}, fallback=config_revision
        ),
    )
    projection = topic_capacity_projection(session, plan.id)
    surface_history, group_history = recent_plan_histories(
        session, plan, limit=RECENT_VOCABULARY_WINDOW
    )
    if is_generic_warmup:
        assert_generic_warmup_question_capacity(
            quality_items, [item.intent for item in group_history]
        )
    return _freeze_assignment_intents(
        session,
        task,
        plan=plan,
        projection=projection,
        assignments=assignments,
        quality_items=quality_items,
        recent_intents=[item.intent for item in surface_history],
        recent_question_intents=[item.intent for item in group_history],
        recent_vocabulary_messages=surface_history,
        is_generic_warmup=is_generic_warmup,
    )


def _freeze_assignment_intents(
    session: Session,
    task: Task,
    *,
    plan: AiGroupContentAllocationPlan,
    projection: TopicCapacityProjection,
    assignments: list[AiPacingAssignment],
    quality_items: list[dict],
    recent_intents: list[AiGroupContentIntent],
    recent_question_intents: list[AiGroupContentIntent],
    recent_vocabulary_messages: list[ContentHistoryMessage],
    is_generic_warmup: bool,
) -> list[dict]:
    updated = list(quality_items)
    active_topic_in_batch = 0
    active_normal_in_batch = 0
    for assignment in assignments:
        source = quality_items[assignment.item_index]
        intent, created = _locked_or_new_intent(
            session,
            task,
            plan=plan,
            assignment=assignment,
            item=source,
            projection=projection,
            active_topic_in_batch=active_topic_in_batch,
            active_normal_in_batch=active_normal_in_batch,
            recent_intents=recent_intents,
            recent_question_intents=recent_question_intents,
            recent_vocabulary_messages=recent_vocabulary_messages,
            is_generic_warmup=is_generic_warmup,
        )
        if created and intent.topic_mode == "configured_topic":
            active_topic_in_batch += 1
        if created:
            active_normal_in_batch += 1
            recent_intents.insert(0, intent)
            recent_question_intents.insert(0, intent)
            recent_vocabulary_messages.insert(0, active_history_message(intent))
        updated[assignment.item_index] = with_content_intent(source, plan, intent)
    return updated


def validate_content_intent_for_gateway(
    session: Session,
    payload: SendMessagePayload,
    *,
    action: Action | None = None,
    remote_boundary: bool = False,
) -> None:
    from .ai_group_content_contract import validate_content_intent_contract

    if remote_boundary:
        _lock_group_surface(session, payload.group_id)
    validate_content_intent_contract(
        session,
        payload,
        action=action,
        capacity_projection=topic_capacity_projection,
        remote_boundary=remote_boundary,
    )


def _lock_daily_target(session: Session, target_id: str) -> None:
    statement = select(TaskGroupDailyTarget).where(TaskGroupDailyTarget.id == target_id)
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    if session.scalar(statement) is None:
        raise ValueError("ai_group_daily_target_missing")


def _lock_group_surface(session: Session, group_id: int) -> None:
    statement = select(TgGroup).where(TgGroup.id == group_id)
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    if session.scalar(statement) is None:
        raise ValueError("ai_group_surface_group_missing")


def _task_day_ledger(session: Session, assignment: AiPacingAssignment) -> TaskDayLedger:
    ledger = session.get(TaskDayLedger, assignment.owner.task_day_ledger_id)
    if ledger is None:
        raise ValueError("ai_group_task_day_ledger_missing")
    return ledger


def _locked_or_new_plan(
    session: Session,
    task: Task,
    *,
    ledger: TaskDayLedger,
    target_operation_target_id: int,
    canonical_group_id: int,
    config_revision: int,
) -> AiGroupContentAllocationPlan:
    route, route_family = _route_contract(task.type_config or {})
    statement = select(AiGroupContentAllocationPlan).where(
        AiGroupContentAllocationPlan.task_day_ledger_id == ledger.id,
        AiGroupContentAllocationPlan.target_operation_target_id
        == target_operation_target_id,
        AiGroupContentAllocationPlan.route_family == route_family,
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    if plan := session.scalar(statement):
        return plan
    rate_bps = normalize_topic_participation_rate(
        effective_topic_rate(task.type_config or {}, ledger.obligation_local_date)
    )
    scope = f"tenant:{task.tenant_id}:group:{canonical_group_id}:route:{route_family}"
    theme = get_daily_vocabulary_theme(scope, ledger.obligation_local_date)
    snapshot = _config_snapshot(task.type_config or {}, route, rate_bps)
    plan = AiGroupContentAllocationPlan(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=target_operation_target_id,
        task_day=ledger.obligation_local_date,
        route_family=route_family,
        surface_scope_key=scope,
        config_revision=config_revision,
        config_snapshot_hash=snapshot,
        topic_rate_bps=rate_bps,
        normal_text_cursor=0,
        question_count=0,
        daily_vocabulary_theme_id=theme.theme_id,
        daily_vocabulary_theme_version=DAILY_THEME_VERSION,
    )
    session.add(plan)
    session.flush()
    return plan


def _route_contract(config: dict[str, Any]) -> tuple[str, str]:
    contract = dict(config.get("_ai_content_contract") or {})
    route = str(
        contract.get("content_route") or config.get("content_route") or ""
    ).strip()
    if route in ADULT_ROUTES or config.get("adult_prompt_enabled") is True:
        return route or "adult_service", "adult"
    return route if route.startswith("general") else "general_chat", "general"


def _config_snapshot(config: dict[str, Any], route: str, rate_bps: int) -> str:
    value = {
        "route": route,
        "topic_rate_bps": rate_bps,
        "topic_directions": config.get("topic_directions") or [],
        "teacher_targets": config.get("teacher_targets") or [],
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _locked_or_new_intent(
    session: Session,
    task: Task,
    *,
    plan: AiGroupContentAllocationPlan,
    assignment: AiPacingAssignment,
    item: dict,
    projection: TopicCapacityProjection,
    active_topic_in_batch: int,
    active_normal_in_batch: int,
    recent_intents: list[AiGroupContentIntent],
    recent_question_intents: list[AiGroupContentIntent],
    recent_vocabulary_messages: list[ContentHistoryMessage],
    is_generic_warmup: bool,
) -> tuple[AiGroupContentIntent, bool]:
    statement = select(AiGroupContentIntent).where(
        AiGroupContentIntent.primary_quantity_slot_id == assignment.owner.id
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    if intent := session.scalar(statement):
        return intent, False
    return (
        _create_intent(
            session,
            task,
            plan=plan,
            assignment=assignment,
            item=item,
            projection=projection,
            active_topic_in_batch=active_topic_in_batch,
            active_normal_in_batch=active_normal_in_batch,
            recent_intents=recent_intents,
            recent_question_intents=recent_question_intents,
            recent_vocabulary_messages=recent_vocabulary_messages,
            is_generic_warmup=is_generic_warmup,
        ),
        True,
    )


def _create_intent(
    session: Session,
    task: Task,
    *,
    plan: AiGroupContentAllocationPlan,
    assignment: AiPacingAssignment,
    item: dict,
    projection: TopicCapacityProjection,
    active_topic_in_batch: int,
    active_normal_in_batch: int,
    recent_intents: list[AiGroupContentIntent],
    recent_question_intents: list[AiGroupContentIntent],
    recent_vocabulary_messages: list[ContentHistoryMessage],
    is_generic_warmup: bool,
) -> AiGroupContentIntent:
    values = _prepare_intent_values(
        session,
        task,
        plan=plan,
        assignment=assignment,
        item=item,
        projection=projection,
        active_topic_in_batch=active_topic_in_batch,
        active_normal_in_batch=active_normal_in_batch,
        recent_intents=recent_intents,
        recent_question_intents=recent_question_intents,
        recent_vocabulary_messages=recent_vocabulary_messages,
        is_generic_warmup=is_generic_warmup,
    )
    intent = new_intent_record(
        task,
        plan,
        assignment=assignment,
        item=item,
        values=values,
        config_revision=content_policy_revision(
            task.type_config or {}, fallback=int(task.config_revision or 1)
        ),
        config_snapshot_hash=_config_snapshot(
            task.type_config or {},
            _route_contract(task.type_config or {})[0],
            plan.topic_rate_bps,
        ),
        target_reference_revision=_target_reference_revision(session, plan),
    )
    session.add(intent)
    session.flush()
    return intent


def _target_reference_revision(
    session: Session,
    plan: AiGroupContentAllocationPlan,
) -> int:
    target = session.get(OperationTarget, plan.target_operation_target_id)
    if target is None:
        raise ValueError("ai_group_content_target_missing")
    return int(target.reference_revision or 1)


def _prepare_intent_values(
    session: Session,
    task: Task,
    *,
    plan: AiGroupContentAllocationPlan,
    assignment: AiPacingAssignment,
    item: dict,
    projection: TopicCapacityProjection,
    active_topic_in_batch: int,
    active_normal_in_batch: int,
    recent_intents: list[AiGroupContentIntent],
    recent_question_intents: list[AiGroupContentIntent],
    recent_vocabulary_messages: list[ContentHistoryMessage],
    is_generic_warmup: bool,
) -> tuple[int, dict, str, Any, Any, list[str], list[str]]:
    ordinal = int(plan.normal_text_cursor or 0) + 1
    plan.normal_text_cursor = ordinal
    slot = dict(item.get("slot") or {})
    relation_kind = _freeze_slot_contract(
        plan, slot, item, recent_question_intents
    )
    decision = _topic_decision(
        task.type_config or {},
        plan,
        ordinal,
        relation_kind,
        projection,
        active_topic_in_batch,
        active_normal_in_batch,
        recent_intents,
    )
    sampled = sample_vocabulary_for_intent(
        task,
        plan,
        ordinal=ordinal,
        slot={
            **slot,
            "persona": _assignment_persona(session, task, assignment),
            "topic_mode": decision.topic_mode,
            "vocabulary_evidence_text": vocabulary_evidence_text(
                item, slot, decision.topic_direction
            ),
        },
        recent_intents=recent_intents,
        recent_vocabulary_messages=recent_vocabulary_messages,
        is_generic_warmup=is_generic_warmup and not bool(item.get("reply_target")),
    )
    surface_terms, normalized = sampled_vocabulary_values(sampled, ordinal)
    return ordinal, slot, relation_kind, decision, sampled, surface_terms, normalized


def _freeze_slot_contract(
    plan: AiGroupContentAllocationPlan,
    slot: dict,
    item: dict,
    recent_question_intents: list[AiGroupContentIntent],
) -> str:
    slot["act_type"] = constrained_act_type(slot, item, recent_question_intents)
    if not str(slot.get("stance") or "").strip():
        raise ValueError("content_intent_stance_required")
    if slot["act_type"] == "question":
        plan.question_count = int(plan.question_count or 0) + 1
    return "reply" if item.get("reply_target") else "direct"


def _assignment_persona(
    session: Session,
    task: Task,
    assignment: AiPacingAssignment,
) -> str:
    coverage_id = str(assignment.owner.task_account_daily_coverage_id or "")
    coverage = (
        session.get(TaskAccountDailyCoverage, coverage_id) if coverage_id else None
    )
    if coverage is None:
        return ""
    personas = (task.type_config or {}).get("account_personas") or {}
    return str(personas.get(str(coverage.account_id)) or "")


def _topic_decision(
    config: dict[str, Any],
    plan: AiGroupContentAllocationPlan,
    ordinal: int,
    relation_kind: str,
    projection: TopicCapacityProjection,
    active_topic_in_batch: int,
    active_normal_in_batch: int,
    recent_intents: list[AiGroupContentIntent],
):
    topics = [
        dict(item)
        for item in config.get("topic_directions") or []
        if isinstance(item, dict)
    ]
    chosen = choose_weighted_topic_direction(topics, recent_intents)
    return decide_topic_mode(
        normal_text_ordinal=ordinal,
        topic_rate_bps=plan.topic_rate_bps,
        has_configured_topics=bool(topics),
        has_human_context=relation_kind == "reply",
        confirmed_normal_count=projection.confirmed_normal_count,
        confirmed_topic_count=projection.confirmed_topic_count,
        unknown_topic_count=projection.unknown_topic_count,
        active_reservations=(
            projection.active_topic_reservations + active_topic_in_batch
        ),
        active_normal_count=(
            projection.active_normal_reservations + active_normal_in_batch
        ),
        chosen_topic_direction=chosen,
    )


__all__ = [
    "TopicCapacityProjection",
    "freeze_content_intents",
    "validate_content_intent_for_gateway",
]
