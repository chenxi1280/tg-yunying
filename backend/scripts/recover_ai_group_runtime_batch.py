from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Action,
    AuditLog,
    ContentMixCycle,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
)
from app.services._common import _now
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.executors import group_ai_chat


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
OPEN_ACTION_STATUSES = ("pending", "claiming", "executing")
TASK_TYPE = "group_ai_chat"


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    task_name: str
    messages_per_round: int
    reply_min_per_round: int
    apply: bool
    expected_state_hash: str
    actor: str
    approval_ref: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int_env(name: str) -> int:
    value = _required_env(name)
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def parse_request() -> RecoveryRequest:
    apply_value = os.getenv("AI_GROUP_RUNTIME_RECOVERY_APPLY", "false").lower()
    if apply_value not in {"true", "false"}:
        raise ValueError("AI_GROUP_RUNTIME_RECOVERY_APPLY must be true or false")
    request = RecoveryRequest(
        task_id=_required_env("AI_GROUP_RUNTIME_RECOVERY_TASK_ID"),
        task_name=_required_env("AI_GROUP_RUNTIME_RECOVERY_TASK_NAME"),
        messages_per_round=_positive_int_env("AI_GROUP_RUNTIME_RECOVERY_MESSAGES_PER_ROUND"),
        reply_min_per_round=_positive_int_env("AI_GROUP_RUNTIME_RECOVERY_REPLY_MIN_PER_ROUND"),
        apply=apply_value == "true",
        expected_state_hash=os.getenv("AI_GROUP_RUNTIME_RECOVERY_EXPECTED_STATE_HASH", "").strip(),
        actor=_required_env("AI_GROUP_RUNTIME_RECOVERY_ACTOR"),
        approval_ref=_required_env("AI_GROUP_RUNTIME_RECOVERY_APPROVAL_REF"),
    )
    if request.reply_min_per_round > request.messages_per_round:
        raise ValueError("reply minimum cannot exceed messages per round")
    if request.apply and len(request.expected_state_hash) != 64:
        raise ValueError("expected state hash is required for apply")
    return request


def _task(session, request: RecoveryRequest, *, lock: bool) -> Task:
    statement = select(Task).where(Task.id == request.task_id)
    if lock:
        statement = statement.with_for_update()
    task = session.scalar(statement)
    if task is None or task.type != TASK_TYPE or task.name != request.task_name:
        raise ValueError("AI group task identity mismatch")
    if task.status not in {"running", "pending"}:
        raise ValueError(f"AI group task is not active: {task.status}")
    return task


def proposed_config(task: Task, request: RecoveryRequest) -> dict[str, Any]:
    cfg = dict(task.type_config or {})
    if str(cfg.get("ai_content_route_v2_enabled", "")).lower() in {"true", "1"}:
        cfg["ai_two_stage_enabled"] = True
        if not str(cfg.get("ai_model", "")).strip():
            cfg["ai_model"] = "gemini-2.5-flash"
        if not str(cfg.get("ai_semantic_reviewer_model", "")).strip():
            cfg["ai_semantic_reviewer_model"] = "gemini-1.5-flash"
    return validated_type_config(
        TASK_TYPE,
        {
            **cfg,
            "messages_per_round_mode": "manual",
            "messages_per_round": request.messages_per_round,
            "reply_min_per_round": request.reply_min_per_round,
        },
    )


def _current_ledger_id(session, task: Task) -> str:
    local_date = datetime.now(LOCAL_TIMEZONE).date()
    value = session.scalar(
        select(TaskDayLedger.id).where(
            TaskDayLedger.task_id == task.id,
            TaskDayLedger.obligation_local_date == local_date,
        )
    )
    return str(value or "")


def _open_action_counts(session, task: Task) -> tuple[int, int]:
    open_filter = (Action.task_id == task.id, Action.status.in_(OPEN_ACTION_STATUSES))
    open_count = int(session.scalar(select(func.count(Action.id)).where(*open_filter)) or 0)
    started_count = int(session.scalar(
        select(func.count(func.distinct(Action.id)))
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(*open_filter, ExecutionAttempt.gateway_call_started_at.is_not(None))
    ) or 0)
    return open_count, started_count


def _ledger_counts(session, task: Task, ledger_id: str) -> dict[str, int]:
    if not ledger_id:
        return {"content_mix_count": 0, "open_quantity_slot_count": 0}
    content_mix_count = int(session.scalar(select(func.count(ContentMixCycle.id)).where(
        ContentMixCycle.task_id == task.id,
        ContentMixCycle.task_day_ledger_id == ledger_id,
    )) or 0)
    open_quantity_slot_count = int(session.scalar(
        select(func.count(TaskGroupDailyMessageSlot.id)).where(
            TaskGroupDailyMessageSlot.task_id == task.id,
            TaskGroupDailyMessageSlot.task_day_ledger_id == ledger_id,
            TaskGroupDailyMessageSlot.state == "open",
        )
    ) or 0)
    return {
        "content_mix_count": content_mix_count,
        "open_quantity_slot_count": open_quantity_slot_count,
    }


def state_snapshot(session, task: Task) -> dict[str, Any]:
    ledger_id = _current_ledger_id(session, task)
    open_count, gateway_started_count = _open_action_counts(session, task)
    config = dict(task.type_config or {})
    return {
        "task_id": task.id,
        "task_name": task.name,
        "status": task.status,
        "config_revision": int(task.config_revision or 0),
        "messages_per_round_mode": config.get("messages_per_round_mode"),
        "messages_per_round": int(config.get("messages_per_round") or 0),
        "reply_min_per_round": int(config.get("reply_min_per_round") or 0),
        "ledger_id": ledger_id,
        "open_action_count": open_count,
        "open_gateway_started_action_count": gateway_started_count,
        **_ledger_counts(session, task, ledger_id),
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _alignment_result(
    blueprint,
    alignment: group_ai_chat.QuantitySlotAlignmentResult,
) -> dict[str, Any]:
    items = list(blueprint.generation.quality_items)
    account_ids = [group_ai_chat._quality_slot_account_id(item) for item in items]
    return {
        "status": "ready" if alignment.code == "aligned" else "blocked",
        "alignment_code": alignment.code,
        "item_count": len(items),
        "matched_quantity_slot_count": alignment.aligned_count,
        "missing_coverage_ids": list(alignment.missing_coverage_ids),
        "missing_extra_count": alignment.missing_extra_count,
        "distinct_item_account_count": len(set(account_ids)),
        "item_account_ids": account_ids,
        "alignment_complete": bool(items) and alignment.code == "aligned",
    }


def simulate_alignment(session, task: Task, config: dict[str, Any]) -> dict[str, Any]:
    savepoint = session.begin_nested()
    try:
        task.type_config = dict(config)
        blueprint = group_ai_chat._prepare_plan_blueprint(session, task)
        if isinstance(blueprint, group_ai_chat.PlanAbort):
            return {"status": "plan_abort", "created": blueprint.created, "last_error": task.last_error}
        target = session.get(TaskGroupDailyTarget, blueprint.facts.coverage.daily_group_target_id)
        if target is None or not target.task_day_ledger_id:
            return {"status": "daily_target_missing"}
        alignment = group_ai_chat._quantity_slot_alignment_for_content_mix(
            session,
            task,
            blueprint,
            target.task_day_ledger_id,
        )
        return _alignment_result(blueprint, alignment)
    finally:
        savepoint.rollback()
        session.expire_all()


def _assert_safe_apply(snapshot: dict[str, Any], request: RecoveryRequest) -> None:
    if snapshot_hash(snapshot) != request.expected_state_hash:
        raise RuntimeError("production recovery state hash changed")
    if snapshot["open_action_count"] or snapshot["open_gateway_started_action_count"]:
        raise RuntimeError("production recovery requires zero open task actions")
    if snapshot["content_mix_count"]:
        raise RuntimeError("production recovery requires zero current-day content mix cycles")


def apply_recovery(session, task: Task, request: RecoveryRequest, snapshot: dict[str, Any]) -> None:
    _assert_safe_apply(snapshot, request)
    before = dict(task.type_config or {})
    after = proposed_config(task, request)
    task.type_config = after
    task.config_revision = int(task.config_revision or 0) + 1
    task.next_run_at = _now()
    task.last_error = "生产临时恢复：已收敛 AI 活群单轮覆盖批次"
    task.updated_at = _now()
    detail = {
        "approval_ref": request.approval_ref,
        "before": {key: before.get(key) for key in ("messages_per_round_mode", "messages_per_round", "reply_min_per_round")},
        "after": {key: after.get(key) for key in ("messages_per_round_mode", "messages_per_round", "reply_min_per_round")},
        "state_hash": request.expected_state_hash,
    }
    session.add(AuditLog(
        tenant_id=task.tenant_id,
        actor=request.actor,
        action="AI活群运行批次临时恢复",
        target_type="task",
        target_id=task.id,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    ))
    session.commit()


def main() -> None:
    request = parse_request()
    import time
    last_err = None
    for attempt in range(5):
        try:
            with SessionLocal() as session:
                task = _task(session, request, lock=request.apply)
                snapshot = state_snapshot(session, task)
                proposed = proposed_config(task, request)
                simulation = simulate_alignment(session, task, proposed)
                result = {
                    "mode": "apply" if request.apply else "preview",
                    "snapshot": snapshot,
                    "state_hash": snapshot_hash(snapshot),
                    "proposed": {
                        "messages_per_round_mode": proposed["messages_per_round_mode"],
                        "messages_per_round": proposed["messages_per_round"],
                        "reply_min_per_round": proposed["reply_min_per_round"],
                    },
                    "simulation": simulation,
                }
                if request.apply:
                    if not simulation.get("alignment_complete"):
                        raise RuntimeError("proposed recovery does not produce complete quantity alignment")
                    task = _task(session, request, lock=True)
                    apply_recovery(session, task, request, state_snapshot(session, task))
                    result["applied"] = True
                else:
                    session.rollback()
                print("AI_GROUP_RUNTIME_BATCH_RECOVERY=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
                return
        except Exception as exc:
            last_err = exc
            time.sleep(1)
    if last_err:
        raise last_err


if __name__ == "__main__":
    main()
