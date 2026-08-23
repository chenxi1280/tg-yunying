from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AccountPacingReservation,
    Action,
    AuditLog,
    ChannelViewDailyMessageTarget,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.direct_action_claims import reconcile_source_pacing_states
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center import orphaned_pacing_target_guard as target_guard
from app.services.task_center.source_pacing import wall_datetime


CONTRACT = "channel_view_source_gap_midnight_recovery_v1"


@dataclass(frozen=True)
class RecoveryRequest:
    task_id: str
    action_id: str
    local_date: date
    rebase_anchor: datetime
    deployed_sha: str


@dataclass(frozen=True)
class RecoveryScope:
    task: Task
    ledger: TaskDayLedger
    action: Action
    owner: ViewFulfillmentObligation
    reservation: AccountPacingReservation
    admission: SourcePacingAdmission
    state: SourcePacingState
    targets: list[dict]


@dataclass(frozen=True)
class TimelinePlan:
    corrected_gap: int
    rebase_at: datetime


def build_manifest(session, request: RecoveryRequest, *, lock: bool = False) -> dict:
    scope = _recovery_scope(session, request, lock=lock)
    corrected_gap = _corrected_gap(scope.ledger, scope.targets)
    plan = TimelinePlan(
        corrected_gap,
        _rebase_at(request, scope, corrected_gap),
    )
    blockers = _blockers(session, request, scope=scope, plan=plan)
    guard = target_guard.attach_target_guards(
        session, [{"action_id": scope.action.id}], lock=lock,
    )[0]["target_guard"]
    if guard["mismatches"]:
        blockers.append("target_guard_mismatch")
    manifest = {
        "contract": CONTRACT,
        "deployed_sha": request.deployed_sha,
        "task": _task_snapshot(scope.task),
        "ledger": _ledger_snapshot(scope.ledger),
        "action": _action_snapshot(scope.action),
        "owner": _owner_snapshot(scope.owner),
        "reservation": _reservation_snapshot(scope.reservation),
        "admission": _admission_snapshot(scope.admission),
        "source_state": _state_snapshot(scope.state),
        "targets": scope.targets,
        "corrected_source_gap_seconds": plan.corrected_gap,
        "rebased_not_before_at": plan.rebase_at.isoformat(),
        "target_guard": guard,
        "blockers": sorted(blockers),
    }
    return {**manifest, "fingerprint": manifest_hash(manifest)}


def manifest_hash(manifest: dict) -> str:
    payload = {key: value for key, value in manifest.items() if key != "fingerprint"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def apply_recovery(
    session,
    request: RecoveryRequest,
    *,
    expected_fingerprint: str,
    actor: str,
    approval_ref: str,
) -> dict:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("actor and approval_ref are required for apply")
    manifest = build_manifest(session, request, lock=True)
    if manifest["fingerprint"] != expected_fingerprint:
        raise RuntimeError("channel view source gap recovery fingerprint changed")
    if manifest["blockers"]:
        raise RuntimeError(f"channel view source gap recovery blocked:{manifest['blockers']}")
    scope = _recovery_scope(session, request, lock=False)
    plan = TimelinePlan(
        int(manifest["corrected_source_gap_seconds"]),
        datetime.fromisoformat(manifest["rebased_not_before_at"]),
    )
    _apply_timeline(scope, plan, expected_fingerprint)
    reconcile_source_pacing_states(session, {scope.admission.source_pacing_state_id})
    session.flush()
    target_guard.assert_target_guards_unchanged(
        session,
        [{
            "action_id": manifest["action"]["id"],
            "target_guard": manifest["target_guard"],
        }],
    )
    _write_audit(session, request, manifest, actor=actor, approval_ref=approval_ref)
    session.flush()
    return _readback(session, request, expected_fingerprint)


def _load_scope(session, request: RecoveryRequest, *, lock: bool):
    statement = (
        select(
            Task, TaskDayLedger, Action, ViewFulfillmentObligation,
            AccountPacingReservation, SourcePacingAdmission,
        )
        .join(TaskDayLedger, TaskDayLedger.task_id == Task.id)
        .join(Action, Action.task_id == Task.id)
        .join(
            ViewFulfillmentObligation,
            ViewFulfillmentObligation.id
            == Action.payload["view_fulfillment_obligation_id"].as_string(),
        )
        .join(AccountPacingReservation, AccountPacingReservation.action_id == Action.id)
        .join(SourcePacingAdmission, SourcePacingAdmission.action_id == Action.id)
        .where(
            Task.id == request.task_id,
            TaskDayLedger.obligation_local_date == request.local_date,
            ViewFulfillmentObligation.task_day_ledger_id == TaskDayLedger.id,
            Action.id == request.action_id,
        )
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    rows = list(session.execute(statement))
    if len(rows) != 1:
        raise ValueError(f"channel view source gap scope count mismatch:{len(rows)}")
    return rows[0]


def _recovery_scope(session, request: RecoveryRequest, *, lock: bool) -> RecoveryScope:
    task, ledger, action, owner, reservation, admission = _load_scope(
        session, request, lock=lock,
    )
    return RecoveryScope(
        task,
        ledger,
        action,
        owner,
        reservation,
        admission,
        _source_state(session, admission.source_pacing_state_id, lock=lock),
        _target_rows(session, ledger.id, lock=lock),
    )


def _source_state(session, state_id: str, *, lock: bool) -> SourcePacingState:
    statement = select(SourcePacingState).where(SourcePacingState.id == state_id)
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    state = session.scalar(statement)
    if state is None:
        raise ValueError("channel view source pacing state missing")
    return state


def _target_rows(session, ledger_id: str, *, lock: bool) -> list[dict]:
    statement = (
        select(ChannelViewDailyMessageTarget)
        .where(
            ChannelViewDailyMessageTarget.task_day_ledger_id == ledger_id,
            ChannelViewDailyMessageTarget.source_state == "active",
        )
        .order_by(ChannelViewDailyMessageTarget.id)
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    return [
        {
            "id": row.id,
            "channel_message_id": row.channel_message_id,
            "effective_target_snapshot": int(row.effective_target_snapshot),
            "due_count": int(row.due_count or 0),
            "updated_at": wall_datetime(row.updated_at).isoformat(),
        }
        for row in session.scalars(statement)
    ]


def _corrected_gap(ledger: TaskDayLedger, targets: list[dict]) -> int:
    plan_total = sum(row["effective_target_snapshot"] for row in targets)
    if plan_total <= 0:
        return 0
    seconds = (wall_datetime(ledger.deadline_at) - wall_datetime(ledger.period_start_at)).total_seconds()
    return max(1, math.floor(seconds / plan_total))


def _rebase_at(request: RecoveryRequest, scope: RecoveryScope, gap: int) -> datetime:
    values = [
        wall_datetime(request.rebase_anchor),
        wall_datetime(scope.admission.planned_release_at),
        wall_datetime(scope.action.pacing_due_at),
        wall_datetime(scope.reservation.due_at),
    ]
    if scope.state.last_call_started_at is not None:
        values.append(
            wall_datetime(scope.state.last_call_started_at)
            + timedelta(
                seconds=max(int(scope.state.last_source_gap_seconds or 0), gap)
            )
        )
    return max(values)


def _blockers(
    session,
    request: RecoveryRequest,
    *,
    scope: RecoveryScope,
    plan: TimelinePlan,
) -> list[str]:
    blockers: list[str] = []
    if scope.task.type != "channel_view" or scope.task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        blockers.append("task_contract_mismatch")
    if scope.task.status != "running" or scope.action.status != "pending":
        blockers.append("runtime_state_mismatch")
    if scope.owner.current_action_id != scope.action.id or scope.owner.status != "pending":
        blockers.append("owner_binding_mismatch")
    if scope.reservation.state not in {"reserved", "bound"} or scope.admission.state != "reserved":
        blockers.append("reservation_state_mismatch")
    if str(dict(scope.action.payload or {}).get("execution_date") or "") != request.local_date.isoformat():
        blockers.append("execution_date_mismatch")
    old_gap = int(scope.admission.source_gap_seconds or 0)
    if not scope.targets or plan.corrected_gap <= 0 or old_gap <= plan.corrected_gap:
        blockers.append("source_gap_not_defective")
    deadline = min(
        wall_datetime(scope.ledger.deadline_at),
        wall_datetime(scope.reservation.source_deadline_at),
    )
    if plan.rebase_at >= deadline:
        blockers.append("rebase_deadline_exceeded")
    gateway_count = _count(
        session,
        ExecutionAttempt.id,
        ExecutionAttempt.action_id == scope.action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )
    fact_count = _count(session, FulfillmentRemoteFact.fact_id, FulfillmentRemoteFact.action_id == scope.action.id)
    view_fact_count = _count(session, ViewRemoteFact.id, ViewRemoteFact.obligation_id == scope.owner.id)
    if gateway_count or fact_count or view_fact_count:
        blockers.append("remote_effect_not_proven_absent")
    reserved_ids = list(session.scalars(select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.source_pacing_state_id == scope.state.id,
        SourcePacingAdmission.state == "reserved",
    )))
    if reserved_ids != [scope.admission.id]:
        blockers.append("shared_source_reserved_scope_mismatch")
    return blockers


def _count(session, column, *filters) -> int:
    return int(session.scalar(select(func.count(column)).where(*filters)) or 0)


def _apply_timeline(scope: RecoveryScope, plan: TimelinePlan, fingerprint: str) -> None:
    scope.action.scheduled_at = plan.rebase_at
    scope.action.release_not_before_at = plan.rebase_at
    scope.action.effective_claim_at = plan.rebase_at
    scope.action.action_version = int(scope.action.action_version or 1) + 1
    result = dict(scope.action.result or {})
    for key in ("error_code", "validation_stage", "call_not_before_at"):
        result.pop(key, None)
    result["source_gap_recovery"] = {
        "contract": CONTRACT,
        "fingerprint": fingerprint,
        "corrected_source_gap_seconds": plan.corrected_gap,
    }
    scope.action.result = result
    scope.owner.release_not_before_at = plan.rebase_at
    scope.reservation.release_not_before_at = plan.rebase_at
    scope.reservation.effective_claim_at = plan.rebase_at
    scope.reservation.version = int(scope.reservation.version or 1) + 1
    scope.admission.call_not_before_at = plan.rebase_at
    scope.admission.source_gap_seconds = plan.corrected_gap
    scope.admission.version = int(scope.admission.version or 1) + 1


def _write_audit(session, request, manifest, *, actor: str, approval_ref: str) -> None:
    task = session.get(Task, request.task_id)
    session.add(AuditLog(
        tenant_id=task.tenant_id,
        actor=actor,
        action="修复频道浏览午夜来源节奏",
        target_type="action",
        target_id=request.action_id,
        detail=json.dumps({
            "approval_ref": approval_ref,
            "contract": CONTRACT,
            "deployed_sha": request.deployed_sha,
            "fingerprint": manifest["fingerprint"],
            "old_source_gap_seconds": manifest["admission"]["source_gap_seconds"],
            "new_source_gap_seconds": manifest["corrected_source_gap_seconds"],
        }, ensure_ascii=False, sort_keys=True),
    ))


def _readback(session, request: RecoveryRequest, fingerprint: str) -> dict:
    action = session.get(Action, request.action_id)
    admission = session.scalar(
        select(SourcePacingAdmission)
        .where(SourcePacingAdmission.action_id == request.action_id)
        .order_by(SourcePacingAdmission.created_at.desc())
        .limit(1)
    )
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == request.action_id,
    ))
    state = session.get(SourcePacingState, admission.source_pacing_state_id)
    audit_count = _count(
        session, AuditLog.id,
        AuditLog.action == "修复频道浏览午夜来源节奏",
        AuditLog.target_id == request.action_id,
        AuditLog.detail.contains(fingerprint),
    )
    return {
        "contract": CONTRACT,
        "fingerprint": fingerprint,
        "action": _action_snapshot(action),
        "reservation": _reservation_snapshot(reservation),
        "admission": _admission_snapshot(admission),
        "source_state": _state_snapshot(state),
        "audit_count": audit_count,
    }


def _task_snapshot(task: Task) -> dict:
    return {"id": task.id, "status": task.status, "type": task.type, "epoch": int(task.task_lifecycle_epoch or 1)}


def _ledger_snapshot(ledger: TaskDayLedger) -> dict:
    return {
        "id": ledger.id,
        "local_date": ledger.obligation_local_date.isoformat(),
        "period_start_at": wall_datetime(ledger.period_start_at).isoformat(),
        "deadline_at": wall_datetime(ledger.deadline_at).isoformat(),
    }


def _action_snapshot(action: Action) -> dict:
    return {
        "id": action.id,
        "status": action.status,
        "version": int(action.action_version or 0),
        "scheduled_at": wall_datetime(action.scheduled_at).isoformat(),
        "release_not_before_at": wall_datetime(action.release_not_before_at).isoformat(),
        "effective_claim_at": wall_datetime(action.effective_claim_at).isoformat(),
        "result": dict(action.result or {}),
    }


def _owner_snapshot(owner: ViewFulfillmentObligation) -> dict:
    return {
        "id": owner.id,
        "status": owner.status,
        "current_action_id": owner.current_action_id or "",
        "release_not_before_at": wall_datetime(owner.release_not_before_at).isoformat(),
    }


def _reservation_snapshot(row: AccountPacingReservation) -> dict:
    return {
        "id": row.id,
        "state": row.state,
        "version": int(row.version or 0),
        "due_at": wall_datetime(row.due_at).isoformat(),
        "release_not_before_at": wall_datetime(row.release_not_before_at).isoformat(),
        "effective_claim_at": wall_datetime(row.effective_claim_at).isoformat(),
        "source_deadline_at": wall_datetime(row.source_deadline_at).isoformat(),
    }


def _admission_snapshot(row: SourcePacingAdmission) -> dict:
    return {
        "id": row.id,
        "state": row.state,
        "version": int(row.version or 0),
        "source_pacing_state_id": row.source_pacing_state_id,
        "planned_release_at": wall_datetime(row.planned_release_at).isoformat(),
        "call_not_before_at": wall_datetime(row.call_not_before_at).isoformat(),
        "source_gap_seconds": int(row.source_gap_seconds or 0),
    }


def _state_snapshot(row: SourcePacingState) -> dict:
    return {
        "id": row.id,
        "version": int(row.version or 0),
        "last_call_started_at": (
            wall_datetime(row.last_call_started_at).isoformat()
            if row.last_call_started_at else ""
        ),
        "last_source_gap_seconds": int(row.last_source_gap_seconds or 0),
        "next_call_not_before_at": (
            wall_datetime(row.next_call_not_before_at).isoformat()
            if row.next_call_not_before_at else ""
        ),
    }


def _validate_runtime_sha(deployed_sha: str) -> None:
    runtime_sha = str(os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or "").lower()
    if len(deployed_sha) != 40 or runtime_sha != deployed_sha.lower():
        raise RuntimeError("channel view source gap deployed SHA mismatch")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile one defective channel-view source gap")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--local-date", type=date.fromisoformat, required=True)
    parser.add_argument("--rebase-anchor", type=datetime.fromisoformat, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    _validate_runtime_sha(args.deployed_sha)
    request = RecoveryRequest(
        args.task_id, args.action_id, args.local_date,
        args.rebase_anchor, args.deployed_sha,
    )
    with SessionLocal() as session:
        if args.mode == "preview":
            result = build_manifest(session, request)
        elif args.mode == "apply":
            result = apply_recovery(
                session, request,
                expected_fingerprint=args.expected_fingerprint,
                actor=args.actor,
                approval_ref=args.approval_ref,
            )
            session.commit()
        else:
            result = _readback(session, request, args.expected_fingerprint)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
