from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    ContentMixCycle,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
)
from app.services._common import _now


def ai_acceptance_statuses(
    session: Session,
    task: Task,
    stats: dict,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    local_date = _local_date(task, now or _now())
    ledger_ids = list(session.scalars(select(TaskDayLedger.id).where(
        TaskDayLedger.task_id == task.id,
        TaskDayLedger.obligation_local_date == local_date,
    )))
    quantity = _quantity_status(session, ledger_ids)
    is_v3 = getattr(task, "fulfillment_contract_version", "legacy_v1") == "fact_first_v3"
    content_mix = (
        _content_mix_status_v3(session, task, ledger_ids)
        if is_v3
        else _content_mix_status_legacy(session, ledger_ids)
    )
    conversation = _conversation_status(stats)
    return {
        "quantity_status": quantity,
        "content_mix_status": content_mix,
        "conversation_quality_status": conversation,
        "acceptance_status": _combined_status(quantity, content_mix, conversation),
    }


def _local_date(task: Task, value: datetime) -> date:
    source = value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return source.astimezone(ZoneInfo(task.timezone or "Asia/Shanghai")).date()


def _quantity_status(session: Session, ledger_ids: list[str]) -> str:
    if not ledger_ids:
        return "evaluating"
    states = list(session.scalars(select(TaskGroupDailyMessageSlot.state).where(
        TaskGroupDailyMessageSlot.task_day_ledger_id.in_(ledger_ids)
    )))
    if not states:
        return "evaluating"
    if any(state == "terminal" for state in states):
        return "missed"
    if any(state == "unknown" for state in states):
        return "at_risk"
    return "met" if all(state == "confirmed" for state in states) else "evaluating"


def _content_mix_status_legacy(session: Session, ledger_ids: list[str]) -> str:
    if not ledger_ids:
        return "evaluating"
    rows = list(session.execute(select(
        ContentMixCycle.settlement_status,
        ContentMixCycle.settlement_outcome,
    ).where(ContentMixCycle.task_day_ledger_id.in_(ledger_ids))))
    if not rows:
        return "evaluating"
    outcomes = [str(outcome or "") for _status, outcome in rows]
    if any(outcome in {"shortfall", "missed"} for outcome in outcomes):
        return "missed"
    if all(status == "settled" and outcome == "met" for status, outcome in rows):
        return "met"
    return "evaluating"


def _content_mix_status_v3(
    session: Session,
    task: Task,
    ledger_ids: list[str],
) -> str:
    if not ledger_ids:
        return "evaluating"
    quantity_slot_ids = tuple(session.scalars(select(TaskGroupDailyMessageSlot.id).where(
        TaskGroupDailyMessageSlot.task_day_ledger_id.in_(ledger_ids)
    )))
    if not quantity_slot_ids:
        return "evaluating"
    rows = list(session.execute(select(
        AiContentWindowPlanSlot.obligation_id,
        AiContentWindowPlanSlot.content_mode,
        AiContentWindowPlanSlot.state,
    ).join(
        AiContentWindowPlan,
        AiContentWindowPlan.id == AiContentWindowPlanSlot.plan_id,
    ).where(
        AiContentWindowPlan.tenant_id == task.tenant_id,
        AiContentWindowPlan.task_id == task.id,
        AiContentWindowPlan.task_lifecycle_epoch == task.task_lifecycle_epoch,
        AiContentWindowPlanSlot.obligation_id.in_(quantity_slot_ids),
        AiContentWindowPlanSlot.state == "gateway_bound",
    )))
    bound_modes = {
        str(obligation_id): str(content_mode or "").strip()
        for obligation_id, content_mode, _state in rows
        if str(content_mode or "").strip()
    }
    return (
        "met"
        if set(quantity_slot_ids) == set(bound_modes)
        else "evaluating"
    )


def _conversation_status(stats: dict) -> str:
    blocker = str(stats.get("conversation_quality_active_blocker") or "")
    if blocker == "context_superseded_requeue":
        return "at_risk"
    if blocker:
        return "blocked"
    return "met" if bool(stats.get("conversation_quality_e4_passed")) else "evaluating"


def _combined_status(*statuses: str) -> str:
    for value in ("missed", "blocked", "at_risk"):
        if value in statuses:
            return value
    return "met" if all(value == "met" for value in statuses) else "evaluating"


__all__ = ["ai_acceptance_statuses"]
