from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AuditLog,
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
)
from app.services._common import _now


RECOVERY_CODE = "reply_ratio_reclassified"
TERMINAL_ACTION_STATUSES = frozenset({"failed", "skipped"})


def build_reply_ratio_recovery_snapshot(
    session: Session,
    *,
    task_ids: tuple[str, ...],
    target_date: date,
    per_task_limit: int,
) -> dict:
    if not task_ids or per_task_limit <= 0:
        raise ValueError("reply ratio recovery scope is invalid")
    tasks = [
        _task_snapshot(session, task_id, target_date, per_task_limit)
        for task_id in task_ids
    ]
    return {
        "target_date": target_date.isoformat(),
        "per_task_limit": per_task_limit,
        "tasks": tasks,
    }


def reply_ratio_recovery_state_hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def apply_reply_ratio_recovery(
    session: Session,
    *,
    snapshot: dict,
    expected_state_hash: str,
    actor: str,
) -> dict:
    if reply_ratio_recovery_state_hash(snapshot) != expected_state_hash:
        raise RuntimeError("reply ratio recovery state hash changed")
    changed: list[dict] = []
    for task_state in snapshot["tasks"]:
        slot_ids = tuple(task_state["candidate_slot_ids"])
        if not slot_ids:
            continue
        changed.append(_apply_task(session, task_state, slot_ids, actor))
    session.commit()
    return {"changed_tasks": changed, "changed_slot_count": sum(item["changed_slot_count"] for item in changed)}


def _task_snapshot(
    session: Session,
    task_id: str,
    target_date: date,
    limit: int,
) -> dict:
    task = session.get(Task, task_id)
    if task is None or task.type != "group_ai_chat":
        raise ValueError(f"AI group task not found: {task_id}")
    ledger = session.scalar(select(TaskDayLedger).where(
        TaskDayLedger.task_id == task_id,
        TaskDayLedger.obligation_local_date == target_date,
    ))
    if ledger is None:
        raise ValueError(f"task-day ledger not found: {task_id}")
    total, reply = _slot_counts(session, task_id, ledger.id)
    round_total, round_reply = _reply_ratio(task)
    desired_reply = total * round_reply // round_total
    excess = max(0, reply - desired_reply)
    candidate_ids = _candidate_slot_ids(
        session, task_id, ledger.id, min(limit, excess),
    )
    return {
        "task_id": task_id,
        "ledger_id": ledger.id,
        "slot_total": total,
        "reply_total": reply,
        "desired_reply_total": desired_reply,
        "excess_reply_total": excess,
        "candidate_slot_ids": candidate_ids,
    }


def _slot_counts(session: Session, task_id: str, ledger_id: str) -> tuple[int, int]:
    total, reply = session.execute(select(
        func.count(ContentMixCycleSlot.id),
        func.count(ContentMixCycleSlot.id).filter(
            ContentMixCycleSlot.relation_kind == "reply",
        ),
    ).join(ContentMixCycle).where(
        ContentMixCycle.task_id == task_id,
        ContentMixCycle.task_day_ledger_id == ledger_id,
    )).one()
    return int(total or 0), int(reply or 0)


def _reply_ratio(task: Task) -> tuple[int, int]:
    config = task.type_config or {}
    total = int(config.get("messages_per_round") or 0)
    reply = int(config.get("reply_min_per_round") or 0)
    if total <= 0 or reply < 0 or reply > total:
        raise ValueError(f"invalid reply ratio: {task.id}")
    return total, reply


def _candidate_slot_ids(
    session: Session,
    task_id: str,
    ledger_id: str,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == ContentMixCycleSlot.current_action_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    statement = select(ContentMixCycleSlot.id).join(ContentMixCycle).join(
        TaskGroupDailyMessageSlot,
        TaskGroupDailyMessageSlot.id == ContentMixCycleSlot.primary_quantity_slot_id,
    ).join(Action, Action.id == ContentMixCycleSlot.current_action_id).where(
        ContentMixCycle.task_id == task_id,
        ContentMixCycle.task_day_ledger_id == ledger_id,
        ContentMixCycleSlot.relation_kind == "reply",
        ContentMixCycleSlot.slot_state == "replan_required",
        TaskGroupDailyMessageSlot.state == "open",
        Action.status.in_(TERMINAL_ACTION_STATUSES),
        ~gateway_started,
    ).order_by(ContentMixCycle.cycle_seq, ContentMixCycleSlot.slot_index).limit(limit)
    return list(session.scalars(statement))


def _apply_task(
    session: Session,
    task_state: dict,
    slot_ids: tuple[str, ...],
    actor: str,
) -> dict:
    slots = list(session.scalars(
        select(ContentMixCycleSlot).where(ContentMixCycleSlot.id.in_(slot_ids)).with_for_update(),
    ))
    if {slot.id for slot in slots} != set(slot_ids):
        raise RuntimeError("reply ratio recovery slot set changed")
    contract_ids: set[str] = set()
    for slot in slots:
        _require_slot_still_guarded(session, slot)
        contract_ids.add(_reclassify_slot(session, slot))
    _write_audit(session, task_state, slot_ids, actor)
    return {
        "task_id": task_state["task_id"],
        "changed_slot_count": len(slots),
        "contract_ids": sorted(contract_ids),
    }


def _require_slot_still_guarded(
    session: Session,
    slot: ContentMixCycleSlot,
) -> None:
    quantity = session.get(TaskGroupDailyMessageSlot, slot.primary_quantity_slot_id)
    action = session.get(Action, slot.current_action_id) if slot.current_action_id else None
    gateway_started = session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == slot.current_action_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1))
    guarded = (
        slot.relation_kind == "reply"
        and slot.slot_state == "replan_required"
        and quantity is not None
        and quantity.state == "open"
        and action is not None
        and action.status in TERMINAL_ACTION_STATUSES
        and gateway_started is None
    )
    if not guarded:
        raise RuntimeError("reply ratio recovery slot state changed")


def _reclassify_slot(session: Session, slot: ContentMixCycleSlot) -> str:
    cycle = session.get(ContentMixCycle, slot.cycle_id)
    if cycle is None:
        raise RuntimeError("reply ratio recovery cycle missing")
    contract = session.scalar(select(ContentMixContract).where(
        ContentMixContract.content_mix_scope_key
        == f"ai:{cycle.task_id}:{cycle.target_operation_target_id}:{cycle.id}:{cycle.config_revision}",
    ))
    if contract is None or contract.reply_planned_count <= 0:
        raise RuntimeError("reply ratio recovery contract invalid")
    session.execute(delete(ContentMixObligation).where(
        ContentMixObligation.assigned_cycle_slot_id == slot.id,
        ContentMixObligation.obligation_kind == "reply",
        ContentMixObligation.status == "pending",
    ))
    contract.reply_planned_count -= 1
    contract.direct_planned_count += 1
    contract.reply_min_required_count = max(0, contract.reply_min_required_count - 1)
    slot.relation_kind = "direct"
    slot.reply_requirement_key = ""
    slot.initial_reply_to_message_id = ""
    slot.current_action_id = None
    slot.terminal_reason = RECOVERY_CODE
    return contract.id


def _write_audit(
    session: Session,
    task_state: dict,
    slot_ids: tuple[str, ...],
    actor: str,
) -> None:
    task = session.get(Task, task_state["task_id"])
    session.add(AuditLog(
        tenant_id=task.tenant_id if task else None,
        actor=actor,
        action="修正AI活群错误冻结回复比例",
        target_type="task",
        target_id=task_state["task_id"],
        detail=json.dumps({
            "reason_code": RECOVERY_CODE,
            "ledger_id": task_state["ledger_id"],
            "slot_ids": list(slot_ids),
            "reply_total_before": task_state["reply_total"],
            "desired_reply_total": task_state["desired_reply_total"],
        }, ensure_ascii=False, sort_keys=True),
        ip_address="",
        created_at=_now(),
    ))


__all__ = [
    "apply_reply_ratio_recovery",
    "build_reply_ratio_recovery_snapshot",
    "reply_ratio_recovery_state_hash",
]
