from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Action, AuditLog, ExecutionAttempt, Task, TaskAccountDailyCoverage
from app.services._common import _now
from app.services.task_center.daily_coverage import release_generation_contract_blocker
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
TASK_TYPE = "group_ai_chat"
RECOVERABLE_ACTION_STATUSES = frozenset({"failed", "skipped"})


@dataclass(frozen=True)
class RecoveryRequest:
    task_ids: tuple[str, ...]
    blocker_code: str
    apply: bool
    expected_state_hash: str
    actor: str
    approval_ref: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def parse_request() -> RecoveryRequest:
    task_ids = tuple(dict.fromkeys(
        item.strip()
        for item in _required_env("AI_GENERATION_CONTRACT_RECOVERY_TASK_IDS").split(",")
        if item.strip()
    ))
    apply_value = os.getenv("AI_GENERATION_CONTRACT_RECOVERY_APPLY", "false").lower()
    if not task_ids:
        raise ValueError("at least one exact task id is required")
    if apply_value not in {"true", "false"}:
        raise ValueError("AI_GENERATION_CONTRACT_RECOVERY_APPLY must be true or false")
    request = RecoveryRequest(
        task_ids=task_ids,
        blocker_code=_required_env("AI_GENERATION_CONTRACT_RECOVERY_BLOCKER_CODE"),
        apply=apply_value == "true",
        expected_state_hash=os.getenv(
            "AI_GENERATION_CONTRACT_RECOVERY_EXPECTED_STATE_HASH", "",
        ).strip(),
        actor=_required_env("AI_GENERATION_CONTRACT_RECOVERY_ACTOR"),
        approval_ref=_required_env("AI_GENERATION_CONTRACT_RECOVERY_APPROVAL_REF"),
    )
    if request.apply and len(request.expected_state_hash) != 64:
        raise ValueError("expected state hash is required for apply")
    return request


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tasks(session: Session, request: RecoveryRequest, *, lock: bool) -> list[Task]:
    statement = select(Task).where(Task.id.in_(request.task_ids)).order_by(Task.id)
    if lock:
        statement = statement.with_for_update()
    tasks = list(session.scalars(statement))
    if {task.id for task in tasks} != set(request.task_ids):
        raise ValueError("AI generation contract recovery task identity mismatch")
    for task in tasks:
        if (
            task.type != TASK_TYPE
            or task.status != "running"
            or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION
        ):
            raise ValueError(f"AI generation contract recovery task state invalid:{task.id}")
    return tasks


def _coverage_rows(
    session: Session,
    request: RecoveryRequest,
    *,
    lock: bool,
) -> list[TaskAccountDailyCoverage]:
    local_date = datetime.now(LOCAL_TIMEZONE).date()
    statement = (
        select(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id.in_(request.task_ids),
            TaskAccountDailyCoverage.coverage_date == local_date,
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_stage == "generation_contract",
            TaskAccountDailyCoverage.blocker_code == request.blocker_code,
            TaskAccountDailyCoverage.reserved_action_id.is_(None),
            TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        )
        .order_by(TaskAccountDailyCoverage.task_id, TaskAccountDailyCoverage.id)
    )
    if lock:
        statement = statement.with_for_update()
    return list(session.scalars(statement))


def _gateway_started_count(session: Session, action_id: str) -> int:
    return int(session.scalar(
        select(func.count(ExecutionAttempt.id)).where(
            ExecutionAttempt.action_id == action_id,
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        )
    ) or 0)


def _row_snapshot(session: Session, row: TaskAccountDailyCoverage) -> tuple[dict, str]:
    action = session.get(Action, row.last_action_id) if row.last_action_id else None
    gateway_started_count = _gateway_started_count(session, action.id) if action else 0
    conflict = ""
    if action is None:
        conflict = "last_action_missing"
    elif action.status not in RECOVERABLE_ACTION_STATUSES:
        conflict = f"last_action_status_invalid:{action.status}"
    elif gateway_started_count:
        conflict = "gateway_already_started"
    result = dict(action.result or {}) if action is not None else {}
    return {
        "coverage_id": row.id,
        "task_id": row.task_id,
        "task_day_ledger_id": row.task_day_ledger_id,
        "group_id": row.group_id,
        "account_id": row.account_id,
        "target_count": row.target_count,
        "confirmed_count": row.confirmed_count,
        "state": row.state,
        "blocker_code": row.blocker_code,
        "blocker_stage": row.blocker_stage,
        "recovery_path": row.recovery_path,
        "last_action_id": row.last_action_id,
        "last_action_status": action.status if action is not None else "missing",
        "last_action_error_code": str(result.get("error_code") or ""),
        "gateway_started_count": gateway_started_count,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }, conflict


def recovery_snapshot(
    session: Session,
    request: RecoveryRequest,
    *,
    lock: bool = False,
) -> dict:
    tasks = _tasks(session, request, lock=lock)
    rows = _coverage_rows(session, request, lock=lock)
    row_snapshots: list[dict] = []
    conflicts: list[dict] = []
    for row in rows:
        item, conflict = _row_snapshot(session, row)
        row_snapshots.append(item)
        if conflict:
            conflicts.append({"coverage_id": row.id, "reason": conflict})
    return {
        "task_ids": [task.id for task in tasks],
        "task_names": [task.name for task in tasks],
        "blocker_code": request.blocker_code,
        "matched_count": len(row_snapshots),
        "rows": row_snapshots,
        "conflicts": conflicts,
    }


def _write_audits(
    session: Session,
    request: RecoveryRequest,
    snapshot: dict,
    state_hash: str,
) -> None:
    rows_by_task: dict[str, list[str]] = {}
    for row in snapshot["rows"]:
        rows_by_task.setdefault(row["task_id"], []).append(row["coverage_id"])
    for task_id, coverage_ids in rows_by_task.items():
        task = session.get(Task, task_id)
        session.add(AuditLog(
            tenant_id=task.tenant_id if task else None,
            actor=request.actor,
            action="AI生成合同覆盖阻塞恢复",
            target_type="task_account_daily_coverage_batch",
            target_id=task_id,
            detail=json.dumps({
                "approval_ref": request.approval_ref,
                "blocker_code": request.blocker_code,
                "coverage_ids": coverage_ids,
                "preview_state_hash": state_hash,
            }, ensure_ascii=False, sort_keys=True),
        ))


def apply_recovery(session: Session, request: RecoveryRequest) -> tuple[dict, list[str]]:
    snapshot = recovery_snapshot(session, request, lock=True)
    state_hash = snapshot_hash(snapshot)
    if state_hash != request.expected_state_hash:
        raise RuntimeError("AI generation contract recovery state hash changed")
    if snapshot["matched_count"] <= 0 or snapshot["conflicts"]:
        raise RuntimeError("AI generation contract recovery preview is not safely applicable")
    timestamp = _now()
    recovered_ids: list[str] = []
    for row in snapshot["rows"]:
        changed = release_generation_contract_blocker(
            session,
            row["coverage_id"],
            approved_reason=request.approval_ref,
            now=timestamp,
        )
        if not changed:
            raise RuntimeError(f"AI generation contract recovery CAS failed:{row['coverage_id']}")
        recovered_ids.append(row["coverage_id"])
    for task_id in request.task_ids:
        task = session.get(Task, task_id)
        task.next_run_at = timestamp
        task.updated_at = timestamp
    _write_audits(session, request, snapshot, state_hash)
    session.commit()
    return snapshot, recovered_ids


def _readback(recovered_ids: list[str], request: RecoveryRequest) -> dict:
    with SessionLocal() as session:
        rows = list(session.scalars(
            select(TaskAccountDailyCoverage)
            .where(TaskAccountDailyCoverage.id.in_(recovered_ids))
            .order_by(TaskAccountDailyCoverage.task_id, TaskAccountDailyCoverage.id)
        ))
        recovered = [{
            "coverage_id": row.id,
            "task_id": row.task_id,
            "state": row.state,
            "blocker_code": row.blocker_code,
            "blocker_stage": row.blocker_stage,
            "reserved_action_id": row.reserved_action_id,
            "last_action_id": row.last_action_id,
            "next_decision_at": row.next_decision_at,
        } for row in rows]
        remaining = recovery_snapshot(session, request)
        return {
            "recovered": recovered,
            "remaining_matching_blocker_count": remaining["matched_count"],
            "task_states": [{
                "task_id": task.id,
                "status": task.status,
                "task_type": task.type,
                "fulfillment_contract_version": task.fulfillment_contract_version,
            } for task in _tasks(session, request, lock=False)],
        }


def main() -> int:
    request = parse_request()
    if not request.apply:
        with SessionLocal() as session:
            snapshot = recovery_snapshot(session, request)
        print(json.dumps({
            "mode": "preview",
            "snapshot": snapshot,
            "state_hash": snapshot_hash(snapshot),
        }, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    with SessionLocal() as session:
        snapshot, recovered_ids = apply_recovery(session, request)
    print(json.dumps({
        "mode": "apply",
        "preview_state_hash": snapshot_hash(snapshot),
        "applied_count": len(recovered_ids),
        "recovered_ids": recovered_ids,
        "readback": _readback(recovered_ids, request),
    }, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
