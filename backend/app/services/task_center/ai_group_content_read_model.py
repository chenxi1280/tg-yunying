from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    OperationTarget,
    Task,
    TaskGroupDailyTarget,
    TgGroup,
)
from app.services._common import _now

from .ai_group_content_projection import (
    plan_intent_remote_states,
)
from .ai_group_topic_policy import (
    NEXT_RATE_DATE_FIELD,
    effective_topic_rate,
    promote_due_topic_rate,
)
from .ai_group_content_intent_support import vocabulary_suppression_reason


def ai_group_content_allocation_summary(session: Session, task: Task) -> dict[str, Any]:
    if task.type != "group_ai_chat":
        return {}
    today = _task_local_date(task)
    config = dict(task.type_config or {})
    promoted = promote_due_topic_rate(config, today)
    if promoted != config:
        config = promoted
    all_plans = list(session.scalars(
        select(AiGroupContentAllocationPlan)
        .where(AiGroupContentAllocationPlan.task_id == task.id)
        .order_by(
            AiGroupContentAllocationPlan.task_day.desc(),
            AiGroupContentAllocationPlan.created_at.desc(),
        )
    ))
    plans = _selected_task_day_plans(all_plans, today)
    current_rate = effective_topic_rate(config, today)
    if plans and plans[0].task_day == today:
        current_rate = plans[0].topic_rate_bps / 10000
    next_rate = effective_topic_rate(config, today + timedelta(days=1))
    summary = _base_summary(config, current_rate, next_rate, today)
    if not plans:
        return summary
    effective_target = _effective_target_for_plan(session, plans[0])
    if effective_target is not None:
        summary["expected_normal_count"] = effective_target
        summary["expected_topic_max_count"] = (
            effective_target * plans[0].topic_rate_bps // 10000
        )
    return _plans_summary(session, config, plans, summary)


def _task_local_date(task: Task):
    now = _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return now.astimezone(ZoneInfo(task.timezone or "Asia/Shanghai")).date()


def _selected_task_day_plans(
    plans: list[AiGroupContentAllocationPlan],
    today,
) -> list[AiGroupContentAllocationPlan]:
    current = [plan for plan in plans if plan.task_day == today]
    if current or not plans:
        return current
    latest_day = plans[0].task_day
    return [plan for plan in plans if plan.task_day == latest_day]


def _effective_target_for_plan(
    session: Session,
    plan: AiGroupContentAllocationPlan,
) -> int | None:
    target = session.get(OperationTarget, plan.target_operation_target_id)
    if target is None:
        return None
    group_id = session.scalar(
        select(TgGroup.id).where(
            TgGroup.tenant_id == plan.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if group_id is None:
        return None
    return session.scalar(
        select(TaskGroupDailyTarget.effective_message_target).where(
            TaskGroupDailyTarget.task_day_ledger_id == plan.task_day_ledger_id,
            TaskGroupDailyTarget.group_id == group_id,
        )
    )


def _plans_summary(
    session: Session,
    config: dict[str, Any],
    plans: list[AiGroupContentAllocationPlan],
    summary: dict[str, Any],
) -> dict[str, Any]:
    plan_ids = [plan.id for plan in plans]
    intents = list(
        session.scalars(
            select(AiGroupContentIntent)
            .where(AiGroupContentIntent.allocation_plan_id.in_(plan_ids))
            .order_by(AiGroupContentIntent.normal_text_ordinal.asc())
        )
    )
    remote = _remote_projection(session, plan_ids)
    planned_topic = sum(intent.topic_mode == "configured_topic" for intent in intents)
    planned_teacher = sum(bool(intent.teacher_target_snapshot) for intent in intents)
    states = sorted(
        {intent.daily_vocabulary_theme_effective_state for intent in intents}
    )
    suppressed = [state for state in states if state.startswith("suppressed_by_")]
    pools = [intent.vocabulary_candidate_count for intent in intents]
    return {
        **summary,
        "allocation_plan_id": plan_ids[0] if len(plan_ids) == 1 else "",
        "allocation_plan_ids": plan_ids,
        "task_day": plans[0].task_day.isoformat(),
        "route_family": plans[0].route_family if len(plans) == 1 else "multiple",
        "route_families": sorted({plan.route_family for plan in plans}),
        "planned_normal_count": len(intents),
        "planned_topic_count": planned_topic,
        "planned_topic_ratio": _ratio(planned_topic, len(intents)),
        **_remote_topic_summary(remote),
        "topic_capacity_state": _capacity_state(config, plans[0].topic_rate_bps, remote),
        "planned_teacher_count": planned_teacher,
        "planned_teacher_ratio": _ratio(planned_teacher, len(intents)),
        "remote_teacher_count": remote["teacher"],
        "remote_teacher_ratio": _ratio(remote["teacher"], remote["normal"]),
        **_vocabulary_summary(config, plans, states, pools, suppressed),
    }


def _single_theme_id(plans: list[AiGroupContentAllocationPlan]) -> int | None:
    values = {plan.daily_vocabulary_theme_id for plan in plans}
    return next(iter(values)) if len(values) == 1 else None


def _remote_topic_summary(remote: dict[str, int]) -> dict[str, Any]:
    numerator = remote["topic"] + remote["unknown_topic"]
    denominator = remote["normal"] + remote["unknown_topic"]
    return {
        "active_topic_reservation_count": remote["active_topic"],
        "unknown_topic_hold_count": remote["unknown_topic"],
        "remote_normal_count": remote["normal"],
        "remote_topic_count": remote["topic"],
        "remote_topic_capacity_numerator": numerator,
        "remote_topic_capacity_denominator": denominator,
        "remote_topic_ratio": _ratio(numerator, denominator),
    }


def _vocabulary_summary(
    config: dict[str, Any],
    plans: list[AiGroupContentAllocationPlan],
    states: list[str],
    pools: list[int],
    suppressed: list[str],
) -> dict[str, Any]:
    return {
        "daily_vocabulary_theme_id": _single_theme_id(plans),
        "daily_vocabulary_theme_ids": sorted(
            {plan.daily_vocabulary_theme_id for plan in plans}
        ),
        "daily_vocabulary_theme_version": plans[0].daily_vocabulary_theme_version,
        "daily_vocabulary_theme_effective_states": states,
        "effective_pool_size_min": min(pools) if pools else 0,
        "effective_pool_size_max": max(pools) if pools else 0,
        "suppression_reason": ",".join(suppressed)
        or vocabulary_suppression_reason(config),
    }


def _base_summary(
    config: dict[str, Any],
    current_rate: float | None,
    next_rate: float | None,
    today,
) -> dict[str, Any]:
    expected = int(config.get("daily_message_target") or 0)
    current_bps = round(float(current_rate or 0) * 10000)
    return {
        "current_task_day_rate": current_rate,
        "next_task_day_rate": next_rate,
        "next_task_day_effective_date": config.get(NEXT_RATE_DATE_FIELD)
        or (today + timedelta(days=1)).isoformat(),
        "expected_normal_count": expected,
        "expected_topic_max_count": expected * current_bps // 10000,
        "allocation_plan_id": "",
        "allocation_plan_ids": [],
        "task_day": today.isoformat(),
        "route_family": "",
        "route_families": [],
        "planned_normal_count": 0,
        "planned_topic_count": 0,
        "planned_topic_ratio": None,
        "active_topic_reservation_count": 0,
        "unknown_topic_hold_count": 0,
        "remote_normal_count": 0,
        "remote_topic_count": 0,
        "remote_topic_capacity_numerator": 0,
        "remote_topic_capacity_denominator": 0,
        "remote_topic_ratio": None,
        "topic_capacity_state": "not_planned",
        "planned_teacher_count": 0,
        "planned_teacher_ratio": None,
        "remote_teacher_count": 0,
        "remote_teacher_ratio": None,
        "daily_vocabulary_theme_id": None,
        "daily_vocabulary_theme_ids": [],
        "daily_vocabulary_theme_version": "",
        "daily_vocabulary_theme_effective_states": [],
        "effective_pool_size_min": 0,
        "effective_pool_size_max": 0,
        "suppression_reason": "",
    }


def _remote_projection(session: Session, plan_ids: list[str]) -> dict[str, int]:
    result = {
        "normal": 0,
        "topic": 0,
        "teacher": 0,
        "active_topic": 0,
        "active_normal": 0,
        "unknown_topic": 0,
    }
    for plan_id in plan_ids:
        for intent, state in plan_intent_remote_states(session, plan_id):
            _add_remote_intent(result, intent, state)
    return result


def _add_remote_intent(result: dict[str, int], intent, state: str) -> None:
    is_topic = intent.topic_mode == "configured_topic"
    if state == "confirmed":
        result["normal"] += 1
        result["topic"] += int(is_topic)
        result["teacher"] += int(bool(intent.teacher_target_snapshot))
    elif is_topic and state == "unknown":
        result["unknown_topic"] += 1
    elif state == "active":
        result["active_normal"] += 1
        result["active_topic"] += int(is_topic)


def _capacity_state(
    config: dict[str, Any], rate_bps: int, remote: dict[str, int]
) -> str:
    if not config.get("topic_directions"):
        return "no_configured_topic"
    if rate_bps <= 0:
        return "disabled"
    protected = remote["topic"] + remote["unknown_topic"] + remote["active_topic"]
    denominator = remote["normal"] + remote["unknown_topic"] + remote["active_normal"]
    return (
        "at_limit"
        if denominator and (protected + 1) * 10000 > (denominator + 1) * rate_bps
        else "available"
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


__all__ = ["ai_group_content_allocation_summary"]
