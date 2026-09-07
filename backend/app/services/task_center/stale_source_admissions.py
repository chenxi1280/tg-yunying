"""Read-only recovery preview and exact-snapshot apply for abandoned reservations."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models import (
    Action, AuditLog, ExecutionAttempt, FulfillmentRemoteFact,
    GatewayRequestEvidenceJournal, SourcePacingAdmission, SourcePacingState,
)

from .direct_action_claims import reconcile_source_pacing_states
from .source_pacing import wall_datetime


TERMINAL_ACTION_STATES = frozenset({"failed", "skipped", "cancelled"})
SAFE_ATTEMPT_STATES = frozenset({"skipped_before_gateway", "failed", "safely_not_executed"})
PREVIEW_VERSION = "stale-source-admissions-v1"


@dataclass(frozen=True)
class RecoveryScope:
    tenant_id: int
    state_ids: tuple[str, ...]

    def __post_init__(self):
        if self.tenant_id <= 0 or not self.state_ids or any(not value for value in self.state_ids):
            raise ValueError("explicit tenant_id and nonempty state_ids are required")
        if len(set(self.state_ids)) != len(self.state_ids):
            raise ValueError("duplicate source state IDs")


def preview_stale_admissions(session: Session, scope: RecoveryScope) -> dict:
    states, admissions, actions, attempts, evidence = _read_scope(session, scope)
    candidates = _candidate_ids(admissions, actions=actions, attempts=attempts, evidence=evidence)
    snapshot = {
        "contract_version": PREVIEW_VERSION,
        "tenant_id": scope.tenant_id,
        "state_ids": sorted(scope.state_ids),
        "states": [_row_snapshot(row) for row in states],
        "admissions": [_row_snapshot(row) for row in admissions],
        "actions": [_action_snapshot(row) for row in actions],
        "attempts": [_attempt_snapshot(row) for row in attempts],
        "evidence": evidence,
        "candidate_ids": sorted(candidates),
        "states_before": {row.id: _json_value(row.next_call_not_before_at) for row in states},
        "states_after": _predicted_tails(states, admissions, excluded=set(candidates)),
    }
    return {**snapshot, "snapshot_hash": _digest(snapshot)}


def apply_stale_admissions(
    session: Session,
    preview: dict,
    *,
    actor: str,
    audit_reference: str,
) -> dict:
    if not actor.strip() or len(actor) > 100 or not audit_reference.strip():
        raise ValueError("actor (1..100 characters) and audit_reference are required")
    expected = {key: value for key, value in preview.items() if key != "snapshot_hash"}
    if preview.get("snapshot_hash") != _digest(expected):
        raise ValueError("invalid recovery preview hash")
    scope = RecoveryScope(preview["tenant_id"], tuple(preview["state_ids"]))
    _lock_scope(session, scope, preview)
    current = preview_stale_admissions(session, scope)
    if current != preview:
        raise ValueError("stale_source_admission_preview_drift")
    ids = set(current["candidate_ids"])
    affected = _cancel_candidates(session, scope, ids=ids)
    reconcile_source_pacing_states(session, affected)
    session.flush()
    readback = preview_stale_admissions(session, scope)
    if readback["states_before"] != current["states_after"]:
        raise RuntimeError("stale_source_admission_readback_mismatch")
    receipt = {
        "preview_hash": preview["snapshot_hash"], "cancelled_ids": sorted(ids),
        "states_before": current["states_before"], "states_after": readback["states_before"],
        "actor": actor, "audit_reference": audit_reference,
    }
    session.add(AuditLog(
        tenant_id=scope.tenant_id, actor=actor, action="cancel_stale_source_admissions",
        target_type="source_pacing_states", target_id=preview["snapshot_hash"],
        detail=json.dumps(receipt, ensure_ascii=False, sort_keys=True),
    ))
    return receipt


def _read_scope(session: Session, scope: RecoveryScope):
    states = list(session.scalars(select(SourcePacingState).where(
        SourcePacingState.tenant_id == scope.tenant_id,
        SourcePacingState.id.in_(scope.state_ids),
    ).order_by(SourcePacingState.id).execution_options(populate_existing=True)))
    if {row.id for row in states} != set(scope.state_ids):
        raise ValueError("source state missing or outside tenant scope")
    admissions = list(session.scalars(select(SourcePacingAdmission).where(
        SourcePacingAdmission.source_pacing_state_id.in_(scope.state_ids),
        SourcePacingAdmission.state == "reserved",
    ).order_by(SourcePacingAdmission.id).execution_options(populate_existing=True)))
    if any(row.tenant_id != scope.tenant_id for row in admissions):
        raise ValueError("admission tenant differs from source state")
    action_ids = {row.action_id for row in admissions if row.action_id}
    actions = list(session.scalars(select(Action).where(Action.id.in_(action_ids))
        .order_by(Action.id).execution_options(populate_existing=True)))
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
    ).order_by(ExecutionAttempt.id).execution_options(populate_existing=True)))
    evidence = _remote_evidence(session, action_ids)
    return states, admissions, actions, attempts, evidence


def _remote_evidence(session: Session, action_ids: set[str]) -> list[dict]:
    evidence = []
    for model in (FulfillmentRemoteFact, GatewayRequestEvidenceJournal):
        key = inspect(model).primary_key[0]
        rows = session.scalars(select(model).where(model.action_id.in_(action_ids)).order_by(key))
        evidence.extend({"table": model.__tablename__, "action_id": row.action_id,
                         "id": getattr(row, key.key), "hash": _digest(_row_snapshot(row))} for row in rows)
    return evidence


def _candidate_ids(admissions, *, actions: list, attempts: list, evidence: list) -> list[str]:
    action_map = {row.id: row for row in actions}
    by_action = defaultdict(list)
    for row in attempts:
        by_action[row.action_id].append(row)
    evidenced = {row["action_id"] for row in evidence}
    return [row.id for row in admissions if _safe_candidate(
        row, action_map.get(row.action_id), attempts=by_action[row.action_id],
        has_evidence=row.action_id in evidenced,
    )]


def _safe_candidate(admission, action, *, attempts: list, has_evidence: bool) -> bool:
    if not _matching_terminal_action(admission, action) or has_evidence:
        return False
    if admission.attempt_id and not any(row.id == admission.attempt_id for row in attempts):
        return False
    return all(_pre_gateway_attempt(row, admission.tenant_id) for row in attempts)


def _matching_terminal_action(admission, action) -> bool:
    return (
        action is not None and action.status in TERMINAL_ACTION_STATES
        and action.tenant_id == admission.tenant_id and action.task_id == admission.task_id
        and (action.result or {}).get("remote_mutation_started") is not True
    )


def _pre_gateway_attempt(row: ExecutionAttempt, tenant_id: int) -> bool:
    return (
        row.tenant_id == tenant_id
        and row.gateway_call_started_at is None and row.after_call_at is not None
        and not row.remote_message_id and row.status in SAFE_ATTEMPT_STATES
        and (row.result_snapshot or {}).get("remote_mutation_started") is not True
    )


def _lock_scope(session: Session, scope: RecoveryScope, preview: dict) -> None:
    # Normal dispatch locks Action before source state, then admission.
    statements = (
        select(Action).where(Action.id.in_([row["id"] for row in preview["actions"]])).order_by(Action.id),
        select(SourcePacingState).where(SourcePacingState.id.in_(scope.state_ids)).order_by(SourcePacingState.id),
        select(SourcePacingAdmission).where(SourcePacingAdmission.source_pacing_state_id.in_(scope.state_ids))
        .order_by(SourcePacingAdmission.id),
        select(ExecutionAttempt).where(ExecutionAttempt.id.in_([row["id"] for row in preview["attempts"]]))
        .order_by(ExecutionAttempt.id),
    )
    for statement in statements:
        list(session.scalars(statement.with_for_update(nowait=True)))


def _cancel_candidates(session: Session, scope: RecoveryScope, *, ids: set[str]) -> set[str]:
    affected = set()
    for row in session.scalars(select(SourcePacingAdmission).where(
        SourcePacingAdmission.id.in_(ids), SourcePacingAdmission.tenant_id == scope.tenant_id,
    )):
        row.state = "cancelled_pre_gateway"
        row.version = int(row.version or 1) + 1
        affected.add(row.source_pacing_state_id)
    return affected


def _predicted_tails(states: list, admissions: list, *, excluded: set[str]) -> dict:
    tails = defaultdict(list)
    changed = set()
    for row in admissions:
        if row.id in excluded:
            changed.add(row.source_pacing_state_id)
            continue
        tail = wall_datetime(row.call_not_before_at) + timedelta(seconds=max(0, row.source_gap_seconds or 0))
        tails[row.source_pacing_state_id].append(tail)
    return {state.id: _json_value(_predicted_state_tail(
        state, tails[state.id], changed=state.id in changed,
    )) for state in states}


def _predicted_state_tail(state: SourcePacingState, tails: list, *, changed: bool):
    if not changed:
        return state.next_call_not_before_at
    candidates = list(tails)
    if state.last_call_started_at is not None:
        candidates.append(wall_datetime(state.last_call_started_at)
                          + timedelta(seconds=max(0, state.last_source_gap_seconds or 0)))
    return max(candidates) if candidates else None


def _action_snapshot(row: Action) -> dict:
    names = ("id", "tenant_id", "task_id", "status", "action_version", "scheduled_at",
             "lease_owner", "lease_expires_at", "claim_owner", "claim_expires_at")
    return {**{name: _json_value(getattr(row, name)) for name in names},
            "result_hash": _digest(row.result or {})}


def _attempt_snapshot(row: ExecutionAttempt) -> dict:
    snapshot = _row_snapshot(row)
    snapshot["result_snapshot"] = _digest(row.result_snapshot or {})
    snapshot.pop("failure_detail", None)
    return snapshot


def _row_snapshot(row) -> dict:
    return {column.key: _json_value(getattr(row, column.key)) for column in inspect(row).mapper.columns}


def _json_value(value):
    if isinstance(value, datetime):
        return wall_datetime(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_value).encode()).hexdigest()
