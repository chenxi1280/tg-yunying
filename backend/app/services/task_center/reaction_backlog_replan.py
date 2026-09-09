"""Audited retirement of broken reaction Actions; the normal planner creates replacements."""
from dataclasses import dataclass
import json

from sqlalchemy import select

from app.models import AccountPacingReservation, Action, AuditLog, FulfillmentRemoteFact, ReactionFulfillmentObligation, Task
from app.services._common import _now
from .direct_action_claims import reconcile_source_pacing_states, settle_fact_first_action_before_gateway
from .reaction_backlog_snapshot import digest, load_backlog_rows, normalize_scope, selected_rows, snapshot_rows, timeline_hash


REPLAN_REASON = "operator_replaced_uncalled_cancelled_backlog"
AUDIT_ACTION = "reaction_backlog_replan_v1"


@dataclass(frozen=True)
class BacklogReplanOperation:
    actor: str
    audit_reference: str
    deployed_sha: str


def apply_reaction_backlog(session, preview, operation):
    scope = normalize_scope(preview["scope"])
    _validate_operation(preview, scope, operation)
    selected_rows(session, Task, Task.id.in_(scope["task_ids"]), lock=True)
    previous = _audit(session, scope, preview["state_hash"])
    if previous is not None:
        return json.loads(previous.detail)["receipt"]
    rows = load_backlog_rows(session, scope, lock=True)
    current = snapshot_rows(session, scope, rows)
    if digest(current) != preview["state_hash"]:
        raise ValueError("reaction_backlog_preview_drift")
    results = _settle_rows(session, rows, current["actions"])
    receipt = {"scope": scope, "state_hash": preview["state_hash"], "results": results,
        "actor": operation.actor, "audit_reference": operation.audit_reference}
    session.add(AuditLog(tenant_id=scope["tenant_id"], actor=operation.actor, action=AUDIT_ACTION,
        target_type="action_backlog", target_id=preview["state_hash"], ip_address="",
        detail=json.dumps({"preview": preview, "receipt": receipt}, sort_keys=True)))
    session.flush()
    return receipt


def _validate_operation(preview, scope, operation):
    if not operation.actor.strip() or not operation.audit_reference.strip():
        raise ValueError("reaction_backlog_audit_identity_missing")
    if scope["deployed_sha"] != operation.deployed_sha:
        raise ValueError("reaction_backlog_runtime_sha_mismatch")
    if preview["state"].get("scope") != scope or digest(preview["state"]) != preview["state_hash"]:
        raise ValueError("reaction_backlog_preview_hash_invalid")


def _audit(session, scope, state_hash):
    return session.scalar(select(AuditLog).where(AuditLog.tenant_id == scope["tenant_id"],
        AuditLog.action == AUDIT_ACTION, AuditLog.target_id == state_hash))


def _settle_rows(session, rows, snapshots):
    _, actions, _, reservations, _ = rows
    reservation_map = {row.action_id: row for row in reservations}
    snapshot_map = {row["action_id"]: row for row in snapshots}
    results, pacing_states = [], set()
    for action in actions:
        reservation = reservation_map[action.id]
        item = snapshot_map[action.id]
        # Restore only the domain input within this transaction, then detach it from the old Action.
        reservation.state = "bound"
        reservation.version += 1
        pacing_states.update(settle_fact_first_action_before_gateway(session, action, now=_now(),
            reason_code=REPLAN_REASON, detail="运营清理从未调用的取消预约积压；原有效义务等待新Action",
            replan_same_obligation=not item["source_expired"]))
        if action.action_dedupe_key:
            action.action_dedupe_key = f"retired_uncalled:{action.id}"
        results.append({**item, "new_action_version": action.action_version,
            "retired_action_dedupe_key": action.action_dedupe_key,
            "new_reservation_version": reservation.version,
            "disposition": "expired" if item["source_expired"] else "replan"})
    reconcile_source_pacing_states(session, pacing_states)
    return results


def verify_reaction_backlog(session, receipt):
    scope = normalize_scope(receipt["scope"])
    audit = _audit(session, scope, receipt["state_hash"])
    if audit is None or json.loads(audit.detail)["receipt"] != receipt:
        raise ValueError("reaction_backlog_audit_readback_mismatch")
    tasks = selected_rows(session, Task, Task.id.in_(scope["task_ids"]))
    if len(tasks) != len(scope["task_ids"]) or any(row.status != "running" for row in tasks):
        raise ValueError("reaction_backlog_task_readback_mismatch")
    replaced = 0
    for item in receipt["results"]:
        action = session.get(Action, item["action_id"])
        reservation = session.get(AccountPacingReservation, item["reservation_id"])
        obligation = session.get(ReactionFulfillmentObligation, item["obligation_id"])
        _verify_old_action(session, action, item)
        replaced += _verify_binding(reservation, obligation, item)
    return {"persistence_status": "persisted_verified", "retired_action_count": len(receipt["results"]),
        "replacement_bound_count": replaced, "state_hash": receipt["state_hash"],
        "business_status": "unproven_requires_reaction_observed"}


def _verify_old_action(session, action, item):
    if (action is None or action.status != "skipped"
            or action.action_version != item["new_action_version"]
            or action.action_dedupe_key != item["retired_action_dedupe_key"]
            or (action.result or {}).get("error_code") != REPLAN_REASON):
        raise ValueError("reaction_backlog_action_readback_mismatch")
    if (action.task_id, action.account_id) != (item["task_id"], item["account_id"]):
        raise ValueError("reaction_backlog_action_identity_changed")
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id))
    if fact is None or fact.fact_kind != "safely_not_executed":
        raise ValueError("reaction_backlog_fact_readback_mismatch")


def _verify_binding(reservation, obligation, item):
    if reservation is None or obligation is None or reservation.version < item["new_reservation_version"]:
        raise ValueError("reaction_backlog_binding_readback_mismatch")
    identity = (reservation.task_id, reservation.account_id, obligation.task_id, obligation.account_id)
    if identity != (item["task_id"], item["account_id"], item["task_id"], item["account_id"]):
        raise ValueError("reaction_backlog_binding_identity_changed")
    if obligation.current_action_id is None and timeline_hash(reservation) != item["timeline_hash"]:
        raise ValueError("reaction_backlog_timeline_readback_mismatch")
    if item["disposition"] == "expired":
        if reservation.state != "missed":
            raise ValueError("reaction_backlog_expired_reservation_reopened")
        return 0
    return _verify_replanned_binding(reservation, obligation, item)


def _verify_replanned_binding(reservation, obligation, item):
    if reservation.state not in {"reserved", "bound", "released", "missed"}:
        raise ValueError("reaction_backlog_reservation_readback_mismatch")
    if obligation.current_action_id == item["action_id"]:
        raise ValueError("reaction_backlog_old_action_rebound")
    if reservation.action_id == item["action_id"]:
        raise ValueError("reaction_backlog_old_reservation_rebound")
    if obligation.current_action_id is None and obligation.status not in {"open", "closed_expired"}:
        raise ValueError("reaction_backlog_obligation_readback_mismatch")
    return int(obligation.current_action_id is not None)
