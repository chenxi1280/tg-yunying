from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    FailureType,
    GatewayRequestEvidenceJournal,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services._common import _now
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION


RECOVERY_CODE = "channel_view_false_target_terminal_v1"
TERMINAL_EVIDENCE_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    deployed_sha: str
    apply: bool
    expected_state_hash: str
    actor: str
    approval_ref: str


def build_manifest(session, request: RecoveryRequest, *, lock: bool) -> dict:
    task = _load_task(session, request.task_id, lock=lock)
    terminal_at = _terminal_at(task)
    failures = _peer_failure_evidence(session, task.id, terminal_at)
    remote_facts = _post_terminal_remote_facts(session, task.id, terminal_at)
    bound_terminal = _bound_terminal_obligations(session, task.id)
    reasons = _candidate_reasons(task, terminal_at, failures, remote_facts)
    return {
        "contract": RECOVERY_CODE,
        "deployed_sha": request.deployed_sha,
        "task": _task_snapshot(task),
        "peer_invalid_failures": failures,
        "post_terminal_remote_facts": remote_facts,
        "bound_terminal_obligations": bound_terminal,
        "candidate": not reasons,
        "blocking_reasons": reasons,
    }


def manifest_hash(manifest: dict) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_recovery(session, request: RecoveryRequest, manifest: dict) -> int:
    _validate_apply(request, manifest)
    task = _load_task(session, request.task_id, lock=True)
    if _task_snapshot(task) != manifest["task"]:
        raise RuntimeError("channel view recovery task state drifted")
    previous_epoch = int(task.task_lifecycle_epoch or 1)
    stats = dict(task.stats or {})
    for key in ("target_terminal", "target_terminal_reason", "target_terminal_at"):
        stats.pop(key, None)
    stats.update({
        "false_target_terminal_recovered_at": _now().isoformat(),
        "false_target_terminal_recovery_code": RECOVERY_CODE,
        "false_target_terminal_previous_epoch": previous_epoch,
    })
    task.status = "running"
    task.next_run_at = _now()
    task.last_error = ""
    task.task_lifecycle_epoch = previous_epoch + 1
    task.stats = stats
    _write_audit(session, request, manifest, previous_epoch)
    return previous_epoch + 1


def readback(request: RecoveryRequest, expected_epoch: int) -> dict:
    with SessionLocal() as session:
        task = _load_task(session, request.task_id, lock=False)
        return {
            "task_id": task.id,
            "status": task.status,
            "task_lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
            "expected_epoch": expected_epoch,
            "next_run_at": task.next_run_at.isoformat() if task.next_run_at else "",
            "target_terminal_present": bool(dict(task.stats or {}).get("target_terminal")),
            "last_error": task.last_error,
        }


def _load_task(session, task_id: str, *, lock: bool) -> Task:
    statement = select(Task).where(Task.id == task_id)
    if lock and session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    task = session.scalar(statement)
    if task is None:
        raise ValueError("channel view recovery task not found")
    if task.type != "channel_view" or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        raise ValueError("channel view recovery task contract mismatch")
    return task


def _terminal_at(task: Task) -> datetime | None:
    value = str(dict(task.stats or {}).get("target_terminal_at") or "")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _peer_failure_evidence(
    session,
    task_id: str,
    terminal_at: datetime | None,
) -> list[dict]:
    statement = (
        select(Action, ExecutionAttempt, GatewayRequestEvidenceJournal)
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .outerjoin(
            GatewayRequestEvidenceJournal,
            GatewayRequestEvidenceJournal.execution_attempt_id == ExecutionAttempt.id,
        )
        .where(
            Action.task_id == task_id,
            Action.action_type == "view_message",
            ExecutionAttempt.failure_type == FailureType.PEER_INVALID.value,
        )
    )
    if terminal_at is not None:
        statement = statement.where(
            ExecutionAttempt.after_call_at >= terminal_at - TERMINAL_EVIDENCE_WINDOW,
        )
    rows = session.execute(statement.order_by(ExecutionAttempt.after_call_at, Action.id))
    return [
        {
            "action_id": action.id,
            "attempt_id": attempt.id,
            "account_id": attempt.account_id,
            "after_call_at": attempt.after_call_at.isoformat() if attempt.after_call_at else "",
            "remote_mutation_state": journal.remote_mutation_state if journal else "missing",
            "journal_state": journal.state if journal else "missing",
        }
        for action, attempt, journal in rows
    ]


def _post_terminal_remote_facts(session, task_id: str, terminal_at: datetime | None) -> dict:
    if terminal_at is None:
        return {"count": 0, "distinct_accounts": 0, "latest_created_at": ""}
    row = session.execute(
        select(
            func.count(ViewRemoteFact.id),
            func.count(func.distinct(ViewRemoteFact.account_id)),
            func.max(ViewRemoteFact.created_at),
        )
        .join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == ViewRemoteFact.obligation_id)
        .join(TaskDayLedger, TaskDayLedger.id == ViewFulfillmentObligation.task_day_ledger_id)
        .where(TaskDayLedger.task_id == task_id, ViewRemoteFact.created_at >= terminal_at)
    ).one()
    return {
        "count": int(row[0] or 0),
        "distinct_accounts": int(row[1] or 0),
        "latest_created_at": row[2].isoformat() if row[2] else "",
    }


def _bound_terminal_obligations(session, task_id: str) -> dict:
    rows = session.execute(
        select(Action.status, func.count(ViewFulfillmentObligation.id))
        .join(Action, Action.id == ViewFulfillmentObligation.current_action_id)
        .join(TaskDayLedger, TaskDayLedger.id == ViewFulfillmentObligation.task_day_ledger_id)
        .where(TaskDayLedger.task_id == task_id, Action.status.in_(("failed", "skipped", "cancelled")))
        .group_by(Action.status)
    )
    return {status: int(count) for status, count in rows}


def _candidate_reasons(task, terminal_at, failures, remote_facts) -> list[str]:
    reasons: list[str] = []
    if task.status != "failed" or not dict(task.stats or {}).get("target_terminal"):
        reasons.append("task_not_false_terminal_shape")
    if terminal_at is None:
        reasons.append("target_terminal_time_missing")
    if not failures:
        reasons.append("peer_invalid_failure_evidence_missing")
    if any(row["remote_mutation_state"] != "false" or row["journal_state"] != "recorded" for row in failures):
        reasons.append("peer_invalid_failure_not_proven_safe")
    if int(remote_facts["count"]) < 1:
        reasons.append("post_terminal_view_remote_fact_missing")
    return reasons


def _task_snapshot(task: Task) -> dict:
    return {
        "task_id": task.id,
        "status": task.status,
        "task_lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "config_revision": int(task.config_revision or 1),
        "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        "last_error": task.last_error,
        "stats_hash": hashlib.sha256(
            json.dumps(task.stats or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _validate_apply(request: RecoveryRequest, manifest: dict) -> None:
    if not request.actor or not request.approval_ref:
        raise ValueError("actor and approval_ref are required for apply")
    if not manifest["candidate"]:
        raise RuntimeError(f"channel view recovery blocked: {manifest['blocking_reasons']}")
    if not request.expected_state_hash or manifest_hash(manifest) != request.expected_state_hash:
        raise RuntimeError("channel view recovery state hash changed")


def _write_audit(session, request, manifest, previous_epoch: int) -> None:
    task = session.get(Task, request.task_id)
    session.add(AuditLog(
        tenant_id=task.tenant_id,
        actor=request.actor,
        action="频道浏览误终态恢复",
        target_type="task",
        target_id=task.id,
        detail=json.dumps({
            "approval_ref": request.approval_ref,
            "deployed_sha": request.deployed_sha,
            "state_hash": request.expected_state_hash,
            "reason_code": RECOVERY_CODE,
            "previous_epoch": previous_epoch,
            "peer_invalid_failure_count": len(manifest["peer_invalid_failures"]),
            "post_terminal_remote_fact_count": manifest["post_terminal_remote_facts"]["count"],
        }, ensure_ascii=False, sort_keys=True),
    ))


def _parse_request(args) -> RecoveryRequest:
    sha = args.deployed_sha.strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError("deployed_sha must be a full 40-character SHA")
    return RecoveryRequest(
        task_id=args.task_id.strip(),
        deployed_sha=sha,
        apply=bool(args.apply),
        expected_state_hash=args.expected_state_hash.strip(),
        actor=args.actor.strip(),
        approval_ref=args.approval_ref.strip(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover one channel-view Task falsely terminalized by account-scoped PEER_INVALID.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-state-hash", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    request = _parse_request(parser.parse_args())
    with SessionLocal() as session:
        manifest = build_manifest(session, request, lock=request.apply)
        result = {"mode": "apply" if request.apply else "preview", "manifest": manifest, "state_hash": manifest_hash(manifest)}
        if request.apply:
            epoch = apply_recovery(session, request, manifest)
            session.commit()
            result["readback"] = readback(request, epoch)
        else:
            session.rollback()
        print("CHANNEL_VIEW_FALSE_TERMINAL_RECOVERY=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
