from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.models import (
    AccountPacingReservation,
    Action,
    AuditLog,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
    ViewFulfillmentObligation,
)
from app.services.task_center.direct_action_claims import (
    reconcile_source_pacing_states,
    release_fact_first_action_reservations,
    settle_fact_first_action_before_gateway,
)
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.source_pacing import wall_datetime


CONTRACT = "orphaned_source_pacing_reconcile_v1"
TERMINAL_REASONS = ("account_task_abandoned", "stale_channel_daily_action")
SAFE_FACT_KIND = "safely_not_executed"


@dataclass(frozen=True)
class ReconcileOptions:
    task_ids: tuple[str, ...]
    terminal_date: date
    current_date: date
    rebase_anchor: datetime
    deployed_sha: str
    apply: bool = False
    expected_state_hash: str = ""
    actor: str = ""
    approval_ref: str = ""


def build_manifest(session, options: ReconcileOptions, *, lock: bool = False) -> dict[str, Any]:
    _validate_tasks(session, options)
    terminal = _terminal_items(session, options, lock=lock)
    current = _current_items(session, options, lock=lock)
    state_ids = _state_ids(terminal, current)
    states = _state_snapshots(session, state_ids, lock=lock)
    planned, plan_blockers = _plan_current(current, states, options.rebase_anchor)
    classified_ids = _classified_admission_ids(terminal, current)
    unclassified = _unclassified_reserved_ids(session, state_ids, classified_ids)
    blockers = [item for item in terminal if item["mode"] == "blocked"]
    return {
        "contract": CONTRACT,
        "deployed_sha": options.deployed_sha,
        "task_ids": list(options.task_ids),
        "terminal_date": options.terminal_date.isoformat(),
        "current_date": options.current_date.isoformat(),
        "rebase_anchor": options.rebase_anchor.isoformat(),
        "terminal_count": len(terminal),
        "current_count": len(planned),
        "state_count": len(states),
        "blocked": bool(blockers or plan_blockers or unclassified),
        "terminal": terminal,
        "current": planned,
        "states": states,
        "blockers": blockers + plan_blockers,
        "unclassified_reserved_admission_ids": unclassified,
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_manifest(options: ReconcileOptions, manifest: dict[str, Any]) -> dict[str, Any]:
    state_hash = manifest_hash(manifest)
    _validate_apply(options, manifest, state_hash)
    with SessionLocal() as session:
        _apply_locked_manifest(session, options, manifest, state_hash=state_hash)
        session.commit()
    return _readback(options, manifest, state_hash)


def _apply_locked_manifest(
    session,
    options: ReconcileOptions,
    expected: dict[str, Any],
    *,
    state_hash: str,
) -> None:
    state_ids = tuple(item["state_id"] for item in expected["states"])
    _lock_states(session, state_ids)
    current = build_manifest(session, options, lock=True)
    if current != expected:
        raise RuntimeError("orphaned source pacing reconciliation drifted")
    affected_state_ids = set(state_ids)
    for item in expected["terminal"]:
        affected_state_ids.update(_apply_terminal_item(session, item))
    for item in expected["current"]:
        _apply_current_item(session, item, state_hash=state_hash)
    reconcile_source_pacing_states(session, affected_state_ids)
    _write_audits(session, options, expected, state_hash=state_hash)


def _apply_terminal_item(session, item: dict[str, Any]) -> set[str]:
    action = session.get(Action, item["action_id"])
    if item["mode"] == "safe_settlement":
        return settle_fact_first_action_before_gateway(
            session,
            action,
            now=wall_datetime(datetime.now().astimezone()),
            reason_code=item["reason_code"],
            detail="历史账号放弃 Action 未收口，补齐 no-Gateway 安全事实链",
        )
    if item["mode"] == "release_safe_fact":
        return release_fact_first_action_reservations(
            session,
            action,
            fact_kind=SAFE_FACT_KIND,
        )
    raise RuntimeError(f"orphaned source pacing terminal mode invalid:{item['mode']}")


def _apply_current_item(session, item: dict[str, Any], *, state_hash: str) -> None:
    action = session.get(Action, item["action_id"])
    admission = session.get(SourcePacingAdmission, item["admission_id"])
    owner = session.get(ViewFulfillmentObligation, item["owner_id"])
    reservation = session.get(AccountPacingReservation, item["reservation_id"])
    not_before = datetime.fromisoformat(item["rebased_not_before_at"])
    action.scheduled_at = not_before
    action.release_not_before_at = not_before
    action.effective_claim_at = not_before
    action.action_version = int(action.action_version or 1) + 1
    action.result = {
        **dict(action.result or {}),
        "source_pacing_rebase": {"contract": CONTRACT, "state_hash": state_hash},
    }
    admission.call_not_before_at = not_before
    admission.version = int(admission.version or 1) + 1
    owner.release_not_before_at = not_before
    reservation.release_not_before_at = not_before
    reservation.effective_claim_at = not_before
    reservation.version = int(reservation.version or 1) + 1


def _terminal_items(session, options: ReconcileOptions, *, lock: bool) -> list[dict[str, Any]]:
    statement = _terminal_statement(options)
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=(Action, ViewFulfillmentObligation, AccountPacingReservation))
    rows = list(session.execute(statement))
    action_ids = tuple(action.id for action, _, _ in rows)
    admissions = _admission_snapshots(session, action_ids, lock=lock)
    facts = _fact_kinds(session, action_ids)
    gateway_ids = _gateway_action_ids(session, action_ids)
    items = [
        _terminal_item(action, owner, reservation, admissions, facts, gateway_ids)
        for action, owner, reservation in rows
    ]
    _assert_unique(items, "action_id")
    return items


def _terminal_statement(options: ReconcileOptions):
    reserved_exists = select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.action_id == Action.id,
        SourcePacingAdmission.state == "reserved",
    ).exists()
    return (
        select(Action, ViewFulfillmentObligation, AccountPacingReservation)
        .join(Task, Task.id == Action.task_id)
        .join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == Action.payload["view_fulfillment_obligation_id"].as_string())
        .join(AccountPacingReservation, AccountPacingReservation.action_id == Action.id)
        .where(
            Action.task_id.in_(options.task_ids),
            Action.task_type == "channel_view",
            Action.action_type == "view_message",
            Action.status == "skipped",
            Action.result["error_code"].as_string().in_(TERMINAL_REASONS),
            Action.payload["execution_date"].as_string() == options.terminal_date.isoformat(),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            ViewFulfillmentObligation.status != "confirmed",
            or_(ViewFulfillmentObligation.current_action_id.is_(None), ViewFulfillmentObligation.current_action_id == Action.id),
            AccountPacingReservation.state.in_(("reserved", "bound")),
            reserved_exists,
        )
        .order_by(Action.task_id, Action.id)
    )


def _terminal_item(action, owner, reservation, admissions, facts, gateway_ids) -> dict[str, Any]:
    action_admissions = admissions.get(action.id, [])
    fact_kinds = facts.get(action.id, [])
    reason = str(dict(action.result or {}).get("error_code") or "")
    safe_admissions = bool(action_admissions) and not any(row["gateway_started"] for row in action_admissions)
    mode = "blocked"
    if safe_admissions and not fact_kinds and action.id not in gateway_ids and reason == "account_task_abandoned":
        mode = "safe_settlement"
    elif safe_admissions and fact_kinds == [SAFE_FACT_KIND]:
        mode = "release_safe_fact"
    return {
        "action_id": action.id,
        "action_version": int(action.action_version or 0),
        "task_id": action.task_id,
        "reason_code": reason,
        "owner_id": owner.id,
        "owner_status": owner.status,
        "owner_current_action_id": owner.current_action_id or "",
        "reservation_id": reservation.id,
        "reservation_state": reservation.state,
        "reservation_version": int(reservation.version or 0),
        "fact_kinds": fact_kinds,
        "action_gateway_started": action.id in gateway_ids,
        "admissions": action_admissions,
        "mode": mode,
    }


def _current_items(session, options: ReconcileOptions, *, lock: bool) -> list[dict[str, Any]]:
    statement = _current_statement(options)
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=(Action, SourcePacingAdmission, ViewFulfillmentObligation, AccountPacingReservation))
    items = [_current_item(*row) for row in session.execute(statement)]
    _assert_unique(items, "action_id")
    return items


def _current_statement(options: ReconcileOptions):
    fact_exists = select(FulfillmentRemoteFact.fact_id).where(FulfillmentRemoteFact.action_id == Action.id).correlate(Action).exists()
    gateway_exists = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).correlate(Action).exists()
    return (
        select(Action, SourcePacingAdmission, ViewFulfillmentObligation, AccountPacingReservation)
        .join(Task, Task.id == Action.task_id)
        .join(SourcePacingAdmission, SourcePacingAdmission.action_id == Action.id)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == Action.payload["view_fulfillment_obligation_id"].as_string())
        .join(AccountPacingReservation, AccountPacingReservation.action_id == Action.id)
        .where(
            Action.task_id.in_(options.task_ids),
            Action.status == "pending",
            Action.payload["execution_date"].as_string() == options.current_date.isoformat(),
            Action.result["source_pacing_rebase"]["contract"].as_string().is_(None),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            SourcePacingAdmission.state == "reserved",
            ExecutionAttempt.gateway_call_started_at.is_(None),
            ViewFulfillmentObligation.current_action_id == Action.id,
            ViewFulfillmentObligation.status != "confirmed",
            AccountPacingReservation.state.in_(("reserved", "bound")),
            ~fact_exists,
            ~gateway_exists,
        )
        .order_by(SourcePacingAdmission.source_pacing_state_id, SourcePacingAdmission.planned_release_at, Action.id)
    )


def _current_item(action, admission, owner, reservation) -> dict[str, Any]:
    return {
        "action_id": action.id,
        "action_version": int(action.action_version or 0),
        "task_id": action.task_id,
        "admission_id": admission.id,
        "admission_version": int(admission.version or 0),
        "state_id": admission.source_pacing_state_id,
        "owner_id": owner.id,
        "reservation_id": reservation.id,
        "reservation_version": int(reservation.version or 0),
        "source_gap_seconds": int(admission.source_gap_seconds or 0),
        "planned_release_at": wall_datetime(admission.planned_release_at).isoformat(),
        "pacing_due_at": wall_datetime(action.pacing_due_at).isoformat(),
        "reservation_due_at": wall_datetime(reservation.due_at).isoformat(),
        "source_deadline_at": wall_datetime(reservation.source_deadline_at).isoformat(),
        "original_call_not_before_at": wall_datetime(admission.call_not_before_at).isoformat(),
        "original_scheduled_at": wall_datetime(action.scheduled_at).isoformat(),
    }


def _plan_current(items, states, anchor: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_state: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_state.setdefault(item["state_id"], []).append(item)
    state_map = {item["state_id"]: item for item in states}
    planned: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for state_id in sorted(by_state):
        rows = sorted(by_state[state_id], key=lambda row: (row["planned_release_at"], row["action_id"]))
        previous_at: datetime | None = None
        previous_gap = 0
        state = state_map[state_id]
        last_at = _optional_datetime(state["last_call_started_at"])
        last_gap = int(state["last_source_gap_seconds"])
        for row in rows:
            gap = int(row["source_gap_seconds"])
            adjacent = wall_datetime(anchor)
            if previous_at is not None:
                adjacent = max(
                    adjacent, previous_at + timedelta(seconds=max(previous_gap, gap)),
                )
            elif last_at is not None:
                adjacent = max(
                    adjacent, last_at + timedelta(seconds=max(last_gap, gap)),
                )
            not_before = max(adjacent, _row_release_floor(row))
            current = {**row, "rebased_not_before_at": not_before.isoformat()}
            if not_before >= datetime.fromisoformat(row["source_deadline_at"]):
                blockers.append({**current, "blocker": "rebased_source_deadline_exceeded"})
            planned.append(current)
            previous_at, previous_gap = not_before, gap
    return planned, blockers


def _row_release_floor(item: dict[str, Any]) -> datetime:
    return max(datetime.fromisoformat(item[key]) for key in (
        "planned_release_at", "pacing_due_at", "reservation_due_at",
    ))


def _state_ids(terminal, current) -> tuple[str, ...]:
    values = {row["state_id"] for row in current}
    for item in terminal:
        values.update(row["state_id"] for row in item["admissions"])
    return tuple(sorted(values))


def _state_snapshots(session, state_ids: tuple[str, ...], *, lock: bool) -> list[dict[str, Any]]:
    if not state_ids:
        return []
    statement = select(SourcePacingState).where(SourcePacingState.id.in_(state_ids)).order_by(SourcePacingState.id)
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    return [{
        "state_id": state.id,
        "version": int(state.version or 0),
        "last_call_started_at": _iso(state.last_call_started_at),
        "last_source_gap_seconds": int(state.last_source_gap_seconds or 0),
        "next_call_not_before_at": _iso(state.next_call_not_before_at),
    } for state in session.scalars(statement)]


def _admission_snapshots(session, action_ids: tuple[str, ...], *, lock: bool) -> dict[str, list[dict[str, Any]]]:
    if not action_ids:
        return {}
    statement = (
        select(SourcePacingAdmission, ExecutionAttempt.gateway_call_started_at)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .where(SourcePacingAdmission.action_id.in_(action_ids), SourcePacingAdmission.state == "reserved")
        .order_by(SourcePacingAdmission.action_id, SourcePacingAdmission.id)
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=SourcePacingAdmission)
    values: dict[str, list[dict[str, Any]]] = {}
    for admission, gateway_at in session.execute(statement):
        values.setdefault(admission.action_id, []).append({
            "admission_id": admission.id,
            "version": int(admission.version or 0),
            "state_id": admission.source_pacing_state_id,
            "attempt_id": admission.attempt_id or "",
            "gateway_started": gateway_at is not None,
            "call_not_before_at": wall_datetime(admission.call_not_before_at).isoformat(),
        })
    return values


def _fact_kinds(session, action_ids: tuple[str, ...]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    if action_ids:
        for action_id, kind in session.execute(select(FulfillmentRemoteFact.action_id, FulfillmentRemoteFact.fact_kind).where(FulfillmentRemoteFact.action_id.in_(action_ids))):
            values.setdefault(action_id, set()).add(kind)
    return {key: sorted(kinds) for key, kinds in values.items()}


def _gateway_action_ids(session, action_ids: tuple[str, ...]) -> set[str]:
    if not action_ids:
        return set()
    return set(session.scalars(select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.action_id.in_(action_ids),
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).distinct()))


def _classified_admission_ids(terminal, current) -> set[str]:
    values = {item["admission_id"] for item in current}
    for item in terminal:
        values.update(row["admission_id"] for row in item["admissions"])
    return values


def _unclassified_reserved_ids(session, state_ids, classified_ids) -> list[str]:
    if not state_ids:
        return []
    all_ids = set(session.scalars(select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.source_pacing_state_id.in_(state_ids),
        SourcePacingAdmission.state == "reserved",
    )))
    return sorted(all_ids - classified_ids)


def _lock_states(session, state_ids: tuple[str, ...]) -> None:
    if state_ids:
        list(session.scalars(select(SourcePacingState).where(
            SourcePacingState.id.in_(state_ids),
        ).order_by(SourcePacingState.id).with_for_update()))


def _write_audits(session, options, manifest, *, state_hash: str) -> None:
    for task_id in options.task_ids:
        terminal_ids = [item["action_id"] for item in manifest["terminal"] if item["task_id"] == task_id]
        current_ids = [item["action_id"] for item in manifest["current"] if item["task_id"] == task_id]
        if not terminal_ids and not current_ids:
            continue
        task = session.get(Task, task_id)
        session.add(AuditLog(
            tenant_id=task.tenant_id,
            actor=options.actor,
            action="来源节奏孤儿预约修复",
            target_type="task",
            target_id=task_id,
            detail=json.dumps({
                "approval_ref": options.approval_ref,
                "contract": CONTRACT,
                "deployed_sha": options.deployed_sha,
                "state_hash": state_hash,
                "terminal_action_ids": terminal_ids,
                "rebased_action_ids": current_ids,
            }, ensure_ascii=False, sort_keys=True),
        ))


def _readback(options, original, state_hash: str) -> dict[str, Any]:
    with SessionLocal() as session:
        remaining = build_manifest(session, options)
        state_ids = tuple(item["state_id"] for item in original["states"])
        cursors = _state_snapshots(session, state_ids, lock=False)
        audits = list(session.scalars(select(AuditLog.id).where(
            AuditLog.actor == options.actor,
            AuditLog.action == "来源节奏孤儿预约修复",
            AuditLog.detail.contains(state_hash),
        )))
    return {"state_hash": state_hash, "remaining": _public_manifest(remaining), "states": cursors, "audit_count": len(audits)}


def _validate_tasks(session, options: ReconcileOptions) -> None:
    tasks = list(session.scalars(select(Task).where(Task.id.in_(options.task_ids))))
    if {task.id for task in tasks} != set(options.task_ids):
        raise ValueError("orphaned source pacing task identity mismatch")
    invalid = [task.id for task in tasks if task.type != "channel_view" or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION]
    if invalid:
        raise ValueError(f"orphaned source pacing task contract mismatch:{invalid}")


def _validate_apply(options, manifest, state_hash: str) -> None:
    if not options.actor or not options.approval_ref:
        raise ValueError("actor and approval_ref are required for apply")
    if options.expected_state_hash != state_hash:
        raise RuntimeError("orphaned source pacing state hash changed")
    if manifest["blocked"]:
        raise RuntimeError("orphaned source pacing reconciliation has blockers")
    if manifest["terminal_count"] + manifest["current_count"] == 0:
        raise RuntimeError("orphaned source pacing reconciliation matched zero rows")


def _validate_runtime_sha(deployed_sha: str) -> None:
    runtime_sha = str(os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or "").lower()
    if len(deployed_sha) != 40 or deployed_sha != runtime_sha:
        raise RuntimeError("orphaned source pacing deployed SHA mismatch")


def _assert_unique(items, key: str) -> None:
    values = [item[key] for item in items]
    if len(values) != len(set(values)):
        raise RuntimeError(f"orphaned source pacing duplicate {key}")


def _iso(value) -> str:
    return wall_datetime(value).isoformat() if value is not None else ""


def _optional_datetime(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in manifest.items() if key not in {"terminal", "current"}},
        "terminal_first": manifest["terminal"][:3],
        "terminal_last": manifest["terminal"][-3:] if len(manifest["terminal"]) > 3 else [],
        "current_first": manifest["current"][:3],
        "current_last": manifest["current"][-3:] if len(manifest["current"]) > 3 else [],
    }
