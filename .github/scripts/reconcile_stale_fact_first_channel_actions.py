from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select

from app.database import SessionLocal
from app.models import (
    AccountPacingReservation,
    Action,
    AuditLog,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    SourcePacingAdmission,
    Task,
    ViewFulfillmentObligation,
)
from app.services._common import _now
from app.services.task_center.direct_action_claims import (
    settle_fact_first_action_before_gateway,
)
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION


CONTRACT = "stale_fact_first_channel_reconcile_v1"
REASON_CODE = "stale_channel_daily_action"
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 200


@dataclass(frozen=True)
class ReconcileOptions:
    task_ids: tuple[str, ...]
    execution_date: date
    deployed_sha: str
    apply: bool = False
    expected_state_hash: str = ""
    actor: str = ""
    approval_ref: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE


def build_manifest(
    session,
    options: ReconcileOptions,
    *,
    action_ids: tuple[str, ...] | None = None,
    lock: bool = False,
) -> dict[str, Any]:
    _validate_tasks(session, options)
    candidates = _candidate_items(
        session,
        options,
        action_ids=action_ids,
        lock=lock,
    )
    scope = _scope_counts(session, options) if action_ids is None else {}
    manifest = {
        "contract": CONTRACT,
        "deployed_sha": options.deployed_sha,
        "task_ids": list(options.task_ids),
        "execution_date": options.execution_date.isoformat(),
        "candidate_count": len(candidates),
        "scope": scope,
        "candidates": candidates,
    }
    if scope and int(scope["blocked_no_fact_no_gateway_count"]) != 0:
        manifest["blocked"] = True
    return manifest


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_manifest(options: ReconcileOptions, manifest: dict[str, Any]) -> dict[str, Any]:
    state_hash = manifest_hash(manifest)
    _validate_apply(options, manifest, state_hash)
    items = list(manifest["candidates"])
    applied = 0
    batch_count = 0
    for batch_count, chunk in enumerate(_chunks(items, options.batch_size), start=1):
        with SessionLocal() as session:
            _apply_chunk(
                session,
                options,
                chunk,
                state_hash=state_hash,
                batch_ordinal=batch_count,
            )
            session.commit()
            applied += len(chunk)
    return {
        "applied_count": applied,
        "batch_count": batch_count,
        "state_hash": state_hash,
        "readback": _readback(options),
    }


def _apply_chunk(
    session,
    options: ReconcileOptions,
    expected_items: list[dict[str, Any]],
    *,
    state_hash: str,
    batch_ordinal: int,
) -> None:
    action_ids = tuple(item["action_id"] for item in expected_items)
    current = build_manifest(
        session,
        options,
        action_ids=action_ids,
        lock=True,
    )
    if current["candidates"] != expected_items:
        raise RuntimeError("stale fact-first channel reconciliation batch drifted")
    now_value = _now()
    for item in expected_items:
        action = session.get(Action, item["action_id"])
        settle_fact_first_action_before_gateway(
            session,
            action,
            now=now_value,
            reason_code=REASON_CODE,
            detail="旧日浏览 Action 在 Gateway 前被 legacy sweep 误终结，补齐安全事实链",
        )
    _write_audits(
        session,
        options,
        expected_items,
        state_hash=state_hash,
        batch_ordinal=batch_ordinal,
    )


def _candidate_items(
    session,
    options: ReconcileOptions,
    *,
    action_ids: tuple[str, ...] | None,
    lock: bool,
) -> list[dict[str, Any]]:
    statement = _candidate_statement(options)
    if action_ids is not None:
        statement = statement.where(Action.id.in_(action_ids))
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(
            of=(Action, ViewFulfillmentObligation, AccountPacingReservation)
        )
    rows = list(session.execute(statement))
    items = [_candidate_item(*row) for row in rows]
    source_snapshots = _source_admission_snapshots(
        session,
        tuple(item["action_id"] for item in items),
        lock=lock,
    )
    for item in items:
        item["source_admissions"] = source_snapshots.get(item["action_id"], [])
    _assert_unique_candidates(items)
    return items


def _candidate_statement(options: ReconcileOptions):
    fact_exists = _fact_exists()
    gateway_exists = _gateway_exists()
    return (
        select(Action, ViewFulfillmentObligation, AccountPacingReservation)
        .join(Task, Task.id == Action.task_id)
        .join(
            ViewFulfillmentObligation,
            ViewFulfillmentObligation.id
            == Action.payload["view_fulfillment_obligation_id"].as_string(),
        )
        .join(
            AccountPacingReservation,
            AccountPacingReservation.action_id == Action.id,
        )
        .where(
            Action.task_id.in_(options.task_ids),
            Action.task_type == "channel_view",
            Action.action_type == "view_message",
            Action.status == "skipped",
            Action.result["error_code"].as_string() == REASON_CODE,
            Action.payload["execution_date"].as_string()
            == options.execution_date.isoformat(),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            ~fact_exists,
            ~gateway_exists,
            ViewFulfillmentObligation.status != "confirmed",
            or_(
                ViewFulfillmentObligation.current_action_id.is_(None),
                ViewFulfillmentObligation.current_action_id == Action.id,
            ),
            AccountPacingReservation.state.in_(("reserved", "bound")),
        )
        .order_by(Action.task_id, Action.id)
    )


def _candidate_item(
    action: Action,
    obligation: ViewFulfillmentObligation,
    reservation: AccountPacingReservation,
) -> dict[str, Any]:
    return {
        "action_id": action.id,
        "action_version": int(action.action_version or 0),
        "task_id": action.task_id,
        "obligation_id": obligation.id,
        "obligation_status": obligation.status,
        "obligation_current_action_id": obligation.current_action_id or "",
        "reservation_id": reservation.id,
        "reservation_state": reservation.state,
        "reservation_version": int(reservation.version or 0),
    }


def _source_admission_snapshots(
    session,
    action_ids: tuple[str, ...],
    *,
    lock: bool,
) -> dict[str, list[dict[str, Any]]]:
    if not action_ids:
        return {}
    statement = (
        select(SourcePacingAdmission, ExecutionAttempt.gateway_call_started_at)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .where(
            SourcePacingAdmission.action_id.in_(action_ids),
            SourcePacingAdmission.state.in_(("reserved", "finished")),
            or_(
                SourcePacingAdmission.attempt_id.is_(None),
                ExecutionAttempt.gateway_call_started_at.is_(None),
            ),
        )
        .order_by(SourcePacingAdmission.action_id, SourcePacingAdmission.id)
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=SourcePacingAdmission)
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for admission, _ in session.execute(statement):
        snapshots.setdefault(str(admission.action_id), []).append({
            "id": admission.id,
            "state": admission.state,
            "version": int(admission.version or 0),
            "attempt_id": admission.attempt_id or "",
        })
    return snapshots


def _scope_counts(session, options: ReconcileOptions) -> dict[str, int]:
    stale = _stale_scope(options)
    fact_exists = _fact_exists()
    gateway_exists = _gateway_exists()
    owner_safe = _owner_safe_exists()
    reservation_safe = _reservation_safe_exists()
    row = session.execute(select(
        func.count(Action.id),
        func.count(Action.id).filter(fact_exists),
        func.count(Action.id).filter(gateway_exists),
        func.count(Action.id).filter(~fact_exists, ~gateway_exists),
        func.count(Action.id).filter(
            ~fact_exists,
            ~gateway_exists,
            or_(~owner_safe, ~reservation_safe),
        ),
    ).select_from(Action).join(Task, Task.id == Action.task_id).where(*stale)).one()
    return {
        "stale_count": int(row[0]),
        "existing_fact_count": int(row[1]),
        "gateway_started_count": int(row[2]),
        "no_fact_no_gateway_count": int(row[3]),
        "blocked_no_fact_no_gateway_count": int(row[4]),
    }


def _stale_scope(options: ReconcileOptions) -> tuple:
    return (
        Action.task_id.in_(options.task_ids),
        Action.task_type == "channel_view",
        Action.action_type == "view_message",
        Action.status == "skipped",
        Action.result["error_code"].as_string() == REASON_CODE,
        Action.payload["execution_date"].as_string()
        == options.execution_date.isoformat(),
        Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
    )


def _fact_exists():
    return select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.action_id == Action.id,
    ).exists()


def _gateway_exists():
    return select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()


def _owner_safe_exists():
    return select(ViewFulfillmentObligation.id).where(
        ViewFulfillmentObligation.id
        == Action.payload["view_fulfillment_obligation_id"].as_string(),
        ViewFulfillmentObligation.status != "confirmed",
        or_(
            ViewFulfillmentObligation.current_action_id.is_(None),
            ViewFulfillmentObligation.current_action_id == Action.id,
        ),
    ).exists()


def _reservation_safe_exists():
    return select(AccountPacingReservation.id).where(
        AccountPacingReservation.action_id == Action.id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ).exists()


def _write_audits(
    session,
    options: ReconcileOptions,
    items: list[dict[str, Any]],
    *,
    state_hash: str,
    batch_ordinal: int,
) -> None:
    for task_id in options.task_ids:
        action_ids = [item["action_id"] for item in items if item["task_id"] == task_id]
        if not action_ids:
            continue
        task = session.get(Task, task_id)
        session.add(AuditLog(
            tenant_id=task.tenant_id,
            actor=options.actor,
            action="fact-first浏览旧日安全事实修复",
            target_type="task",
            target_id=task_id,
            detail=json.dumps({
                "approval_ref": options.approval_ref,
                "batch_ordinal": batch_ordinal,
                "contract": CONTRACT,
                "deployed_sha": options.deployed_sha,
                "execution_date": options.execution_date.isoformat(),
                "state_hash": state_hash,
                "action_ids": action_ids,
            }, ensure_ascii=False, sort_keys=True),
        ))


def _readback(options: ReconcileOptions) -> dict[str, Any]:
    with SessionLocal() as session:
        remaining = build_manifest(session, options)
        audit_count = session.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.actor == options.actor,
            AuditLog.action == "fact-first浏览旧日安全事实修复",
            AuditLog.detail.contains(options.expected_state_hash),
        ))
    return {
        "remaining_candidate_count": remaining["candidate_count"],
        "remaining_scope": remaining["scope"],
        "audit_count": int(audit_count or 0),
    }


def _validate_tasks(session, options: ReconcileOptions) -> None:
    rows = list(session.scalars(select(Task).where(Task.id.in_(options.task_ids))))
    if {task.id for task in rows} != set(options.task_ids):
        raise ValueError("stale fact-first channel task identity mismatch")
    invalid = [task.id for task in rows if (
        task.type != "channel_view"
        or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION
    )]
    if invalid:
        raise ValueError(f"stale fact-first channel task contract mismatch: {invalid}")


def _validate_apply(
    options: ReconcileOptions,
    manifest: dict[str, Any],
    state_hash: str,
) -> None:
    if not options.actor or not options.approval_ref:
        raise ValueError("actor and approval_ref are required for apply")
    if not options.expected_state_hash or options.expected_state_hash != state_hash:
        raise RuntimeError("stale fact-first channel reconciliation state hash changed")
    if manifest.get("blocked"):
        raise RuntimeError("stale fact-first channel reconciliation has blocked rows")
    if not manifest["candidates"]:
        raise RuntimeError("stale fact-first channel reconciliation matched zero actions")


def _validate_runtime_sha(deployed_sha: str) -> None:
    runtime_sha = str(os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or "").lower()
    if len(deployed_sha) != 40 or deployed_sha != runtime_sha:
        raise RuntimeError("stale fact-first channel deployed SHA mismatch")


def _assert_unique_candidates(items: list[dict[str, Any]]) -> None:
    action_ids = [item["action_id"] for item in items]
    if len(action_ids) != len(set(action_ids)):
        raise RuntimeError("stale fact-first channel candidate ownership is not unique")


def _chunks(items: list[dict[str, Any]], size: int):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    candidates = manifest["candidates"]
    return {
        **{key: value for key, value in manifest.items() if key != "candidates"},
        "candidate_first": candidates[:3],
        "candidate_last": candidates[-3:] if len(candidates) > 3 else [],
    }


def _parse_options(args) -> ReconcileOptions:
    task_ids = tuple(sorted(set(args.task_id)))
    deployed_sha = args.deployed_sha.strip().lower()
    _validate_runtime_sha(deployed_sha)
    if not task_ids:
        raise ValueError("at least one task_id is required")
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    return ReconcileOptions(
        task_ids=task_ids,
        execution_date=date.fromisoformat(args.execution_date),
        deployed_sha=deployed_sha,
        apply=bool(args.apply),
        expected_state_hash=args.expected_state_hash.strip().lower(),
        actor=args.actor.strip(),
        approval_ref=args.approval_ref.strip(),
        batch_size=args.batch_size,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile exact stale fact-first channel Actions without a Gateway call."
    )
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--execution-date", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-state-hash", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    options = _parse_options(parser.parse_args())
    with SessionLocal() as session:
        manifest = build_manifest(session, options)
        state_hash = manifest_hash(manifest)
        session.rollback()
    result = {
        "mode": "apply" if options.apply else "preview",
        "manifest": _public_manifest(manifest),
        "state_hash": state_hash,
    }
    if options.apply:
        result["apply"] = apply_manifest(options, manifest)
    print("STALE_FACT_FIRST_CHANNEL_RECONCILE=" + json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
