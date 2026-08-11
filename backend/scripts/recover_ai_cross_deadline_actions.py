from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.datetime_compat import is_after_or_equal
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION


RECOVERY_CODE = "ai_cross_deadline_action_recovery"


@dataclass(frozen=True)
class RecoveryRequest:
    task_ids: tuple[str, ...]
    deployed_sha: str
    apply: bool
    expected_state_hash: str
    actor: str
    approval_ref: str


def build_manifest(session, request: RecoveryRequest, *, lock: bool) -> dict:
    _validate_tasks(session, request.task_ids)
    rows = _candidate_rows(session, request.task_ids, lock=lock)
    now_value = _now()
    candidates = [
        _candidate_item(action, quantity, ledger, now_value=now_value)
        for action, quantity, ledger in rows
    ]
    return {
        "contract": RECOVERY_CODE,
        "deployed_sha": request.deployed_sha,
        "task_ids": list(request.task_ids),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def manifest_hash(manifest: dict) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_recovery(session, request: RecoveryRequest, manifest: dict) -> list[str]:
    if not request.actor.strip() or not request.approval_ref.strip():
        raise ValueError("actor and approval_ref are required for apply")
    if not request.expected_state_hash or manifest_hash(manifest) != request.expected_state_hash:
        raise RuntimeError("AI cross-deadline recovery state hash changed")
    if not manifest["candidates"]:
        raise RuntimeError("AI cross-deadline recovery matched zero actions")
    action_ids: list[str] = []
    for item in manifest["candidates"]:
        action = session.get(Action, item["action_id"])
        if action is None or int(action.action_version or 0) != item["action_version"]:
            raise RuntimeError("AI cross-deadline recovery action drifted")
        _terminalize_or_replan(session, action, item["recovery_mode"])
        action_ids.append(action.id)
    _write_audits(session, request, manifest, action_ids)
    return action_ids


def readback(request: RecoveryRequest, action_ids: list[str]) -> dict:
    with SessionLocal() as session:
        remaining = build_manifest(session, request, lock=False)
        rows = list(session.execute(
            select(Action.id, Action.status, Action.action_version)
            .where(Action.id.in_(action_ids))
            .order_by(Action.id)
        ))
        return {
            "remaining_candidate_count": remaining["candidate_count"],
            "actions": [
                {"action_id": row.id, "status": row.status, "action_version": row.action_version}
                for row in rows
            ],
            "neighbor_scope": "only exact task_ids and preview action_ids were writable",
        }


def _validate_tasks(session, task_ids: tuple[str, ...]) -> None:
    tasks = list(session.scalars(select(Task).where(Task.id.in_(task_ids))))
    matched = {task.id for task in tasks}
    if matched != set(task_ids):
        raise ValueError("AI cross-deadline recovery task identity mismatch")
    invalid = [
        task.id
        for task in tasks
        if task.type != "group_ai_chat"
        or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION
    ]
    if invalid:
        raise ValueError(f"AI cross-deadline recovery task contract mismatch: {invalid}")


def _candidate_rows(session, task_ids: tuple[str, ...], *, lock: bool) -> list[tuple]:
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    statement = (
        select(Action, TaskGroupDailyMessageSlot, TaskDayLedger)
        .join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id,
        )
        .join(
            TaskDayLedger,
            TaskDayLedger.id == TaskGroupDailyMessageSlot.task_day_ledger_id,
        )
        .where(
            Action.task_id.in_(task_ids),
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "pending",
            Action.scheduled_at >= TaskDayLedger.deadline_at,
            Action.claim_owner == "",
            Action.lease_owner == "",
            ~gateway_started,
        )
        .order_by(Action.task_id, TaskDayLedger.deadline_at, Action.scheduled_at, Action.id)
    )
    if lock and session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(of=Action)
    rows = list(session.execute(statement))
    return [row for row in rows if not _payload_gateway_started(row[0])]


def _candidate_item(
    action: Action,
    quantity: TaskGroupDailyMessageSlot,
    ledger: TaskDayLedger,
    *,
    now_value: datetime,
) -> dict:
    expired = is_after_or_equal(now_value, ledger.deadline_at)
    return {
        "action_id": action.id,
        "action_version": int(action.action_version or 0),
        "task_id": action.task_id,
        "ledger_id": ledger.id,
        "quantity_slot_id": quantity.id,
        "content_mix_cycle_slot_id": str(action.content_mix_cycle_slot_id or ""),
        "coverage_id": str(quantity.task_account_daily_coverage_id or ""),
        "scheduled_at": action.scheduled_at.isoformat(),
        "deadline_at": ledger.deadline_at.isoformat(),
        "recovery_mode": "terminal_after_deadline" if expired else "replan_before_deadline",
    }


def _terminalize_or_replan(session, action: Action, recovery_mode: str) -> None:
    code = (
        "ai_task_day_deadline_expired"
        if recovery_mode == "terminal_after_deadline"
        else "ai_schedule_beyond_task_day_deadline"
    )
    detail = (
        "AI 任务日已截止，跨日 Action 已终结且不再补发"
        if recovery_mode == "terminal_after_deadline"
        else "AI Action 排期越过任务日截止，释放原数量槽并按当前 due 重新规划"
    )
    dispatcher._skip(action, code, detail)
    action.action_version = int(action.action_version or 0) + 1
    dispatcher._sync_action_coverage_state(session, action)
    dispatcher._sync_action_content_mix_state(session, action)


def _write_audits(
    session,
    request: RecoveryRequest,
    manifest: dict,
    action_ids: list[str],
) -> None:
    for task_id in request.task_ids:
        task_action_ids = [
            item["action_id"]
            for item in manifest["candidates"]
            if item["task_id"] == task_id
        ]
        if not task_action_ids:
            continue
        task = session.get(Task, task_id)
        session.add(AuditLog(
            tenant_id=task.tenant_id,
            actor=request.actor,
            action="AI活群跨任务日Action恢复",
            target_type="task",
            target_id=task_id,
            detail=json.dumps({
                "approval_ref": request.approval_ref,
                "deployed_sha": request.deployed_sha,
                "state_hash": request.expected_state_hash,
                "action_ids": sorted(set(task_action_ids) & set(action_ids)),
                "reason_code": RECOVERY_CODE,
            }, ensure_ascii=False, sort_keys=True),
        ))


def _payload_gateway_started(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    return bool(result.get("gateway_call_started_at"))


def _parse_request(args) -> RecoveryRequest:
    task_ids = tuple(sorted(set(args.task_id)))
    if not task_ids:
        raise ValueError("at least one task_id is required")
    deployed_sha = args.deployed_sha.strip().lower()
    if len(deployed_sha) != 40 or any(char not in "0123456789abcdef" for char in deployed_sha):
        raise ValueError("deployed_sha must be a full 40-character SHA")
    return RecoveryRequest(
        task_ids=task_ids,
        deployed_sha=deployed_sha,
        apply=bool(args.apply),
        expected_state_hash=args.expected_state_hash.strip(),
        actor=args.actor.strip(),
        approval_ref=args.approval_ref.strip(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover exact pre-Gateway AI Actions scheduled beyond their ledger deadline.")
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-state-hash", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    request = _parse_request(parser.parse_args())
    with SessionLocal() as session:
        manifest = build_manifest(session, request, lock=request.apply)
        result = {
            "mode": "apply" if request.apply else "preview",
            "manifest": manifest,
            "state_hash": manifest_hash(manifest),
        }
        if request.apply:
            action_ids = apply_recovery(session, request, manifest)
            session.commit()
            result["readback"] = readback(request, action_ids)
        else:
            session.rollback()
        print("AI_CROSS_DEADLINE_RECOVERY=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
