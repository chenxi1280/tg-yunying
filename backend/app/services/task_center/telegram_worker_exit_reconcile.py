"""Audited ACK of exact legacy calls whose original PID 1 has exited."""
from dataclasses import dataclass
from datetime import timezone
import json
import re

from sqlalchemy import select

from app.common.state_hash import canonical_state_hash
from app.models import (AccountBehaviorBudgetReservation, AccountPoolConcurrencyLease,
    Action, AuditLog, ExecutionAttempt, RemoteInvocationFence)
from app.services._common import _now, audit
from app.timezone import as_beijing_aware

from .engagement_action_classes import ACTION_CLASS_BY_TYPE
from .task_retirement import ENGAGEMENT_TYPES
from .telegram_worker_exit_evidence import proof_for_attempt, validate_exit_proofs


ACK_AUDIT = "历史Telegram原进程退出对账"
ACK_TARGET = "telegram_worker_exit"
ACK_SCHEMA = 1
TRANSPORT_FIELDS = frozenset({"transport_termination_state", "transport_termination_observed_at",
    "transport_termination_exited_at", "transport_termination_evidence"})
ISSUED_STATES = frozenset({"gateway_call_started", "result_unknown", "failed", "permanent_failed"})


@dataclass(frozen=True)
class WorkerExitOperation:
    actor: str
    audit_reference: str
    deployed_sha: str


def preview_worker_exits(session, spec):
    tenant_id, attempt_ids = _validate_spec(spec)
    proofs = validate_exit_proofs(spec["evidence"], observed_at=_now())
    rows = _exact_rows(session, tenant_id, attempt_ids)
    _require_legacy_resources(session, attempt_ids)
    state = {"spec": spec, "attempts": [_preview_row(action, attempt, proofs) for action, attempt in rows]}
    return {"schema_version": ACK_SCHEMA, "state": state, "state_hash": canonical_state_hash(state)}


def apply_worker_exits(session, preview, operation):
    _validate_operation(preview, operation)
    prior = _prior_receipt(session, preview, operation)
    if prior is not None:
        verify_worker_exits(session, prior)
        return prior
    spec = preview["state"]["spec"]
    tenant_id, attempt_ids = _validate_spec(spec)
    rows = _exact_rows(session, tenant_id, attempt_ids, lock=True)
    current = preview_worker_exits(session, spec)
    if current["state_hash"] != preview["state_hash"]:
        raise ValueError("worker_exit_preview_conflict")
    proofs = validate_exit_proofs(spec["evidence"], observed_at=_now())
    observed_at = _stamp(_now())
    receipts = [_acknowledge(action, attempt, proofs=proofs, preview_hash=preview["state_hash"],
        operation=operation, observed_at=observed_at) for action, attempt in rows]
    receipt = {"schema_version": ACK_SCHEMA, "tenant_id": tenant_id,
        "preview_hash": preview["state_hash"], "deployed_sha": operation.deployed_sha,
        "audit_reference": operation.audit_reference, "observed_at": observed_at,
        "evidence": spec["evidence"], "attempts": receipts}
    audit(session, tenant_id=tenant_id, actor=operation.actor, action=ACK_AUDIT,
        target_type=ACK_TARGET, target_id=preview["state_hash"],
        detail=json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    session.flush()
    return receipt


def verify_worker_exits(session, receipt):
    audit_row = _receipt_audit(session, receipt["tenant_id"], receipt["preview_hash"])
    if audit_row is None or json.loads(audit_row.detail) != receipt:
        raise ValueError("worker_exit_receipt_not_audited")
    expected = {row["attempt_id"]: row for row in receipt["attempts"]}
    rows = _exact_rows(session, receipt["tenant_id"], tuple(expected))
    for action, attempt in rows:
        before = expected[attempt.id]
        if (_business_hash(action, attempt) != before["business_hash"]
                or canonical_state_hash(attempt.result_snapshot) != before["after_snapshot_hash"]):
            raise ValueError("worker_exit_readback_mismatch")
    return {"acknowledged": len(rows), "business_fields_preserved": True,
        "preview_hash": receipt["preview_hash"]}


def _preview_row(action, attempt, proofs):
    _require_issued_original(action, attempt)
    proof = proof_for_attempt(proofs, attempt)
    return {"attempt_id": attempt.id, "action_id": action.id, "worker_id": attempt.worker_id,
        "call_at": _stamp(attempt.gateway_call_started_at), "status": attempt.status,
        "business_hash": _business_hash(action, attempt),
        "snapshot_hash": canonical_state_hash(attempt.result_snapshot),
        "exit_evidence_hash": proof.evidence_hash, "exited_at": _stamp(proof.exited_at)}


def _require_issued_original(action, attempt):
    if (action.task_type not in ENGAGEMENT_TYPES or action.action_type not in ACTION_CLASS_BY_TYPE
            or attempt.status not in ISSUED_STATES or attempt.remote_message_id):
        raise ValueError("worker_exit_legacy_attempt_ineligible")
    if (action.tenant_id, action.account_id, action.task_lifecycle_epoch) != (
            attempt.tenant_id, attempt.account_id, attempt.task_lifecycle_epoch):
        raise ValueError("worker_exit_original_owner_conflict")
    if (attempt.result_snapshot or {}).get("transport_termination_state") == "acknowledged":
        raise ValueError("worker_exit_attempt_already_acknowledged")


def _acknowledge(action, attempt, *, proofs, preview_hash, operation, observed_at):
    proof = proof_for_attempt(proofs, attempt)
    before = _preview_row(action, attempt, proofs)
    attempt.result_snapshot = {**dict(attempt.result_snapshot or {}),
        "transport_termination_state": "acknowledged", "transport_termination_observed_at": observed_at,
        "transport_termination_exited_at": _stamp(proof.exited_at),
        "transport_termination_evidence": {"kind": "original_docker_pid1_exit", "hash": proof.evidence_hash,
            "source_host": proof.source_host, "container_id": proof.container_id,
            "preview_hash": preview_hash, "audit_reference": operation.audit_reference}}
    return {**before, "after_snapshot_hash": canonical_state_hash(attempt.result_snapshot)}


def _business_hash(action, attempt):
    identity = {name: getattr(attempt, name) for name in ("id", "tenant_id", "action_id", "account_id",
        "worker_id", "task_lifecycle_epoch", "status", "remote_message_id", "failure_type", "failure_detail")}
    times = {name: _stamp(getattr(attempt, name)) for name in (
        "before_call_at", "gateway_call_started_at", "after_call_at")}
    action_state = {name: getattr(action, name) for name in ("id", "tenant_id", "task_id", "task_type",
        "action_type", "account_id", "task_lifecycle_epoch", "status", "payload", "result")}
    action_state["pacing_due_at"] = _stamp(action.pacing_due_at)
    snapshot = {key: value for key, value in (attempt.result_snapshot or {}).items()
        if key not in TRANSPORT_FIELDS}
    return canonical_state_hash({"identity": identity, "times": times, "action": action_state,
        "business_snapshot": snapshot})


def _exact_rows(session, tenant_id, attempt_ids, *, lock=False):
    query = (select(Action, ExecutionAttempt).join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(ExecutionAttempt.tenant_id == tenant_id, ExecutionAttempt.id.in_(attempt_ids))
        .order_by(Action.id, ExecutionAttempt.id))
    if lock:
        query = query.with_for_update(nowait=True, of=(Action, ExecutionAttempt))
    rows = list(session.execute(query.execution_options(populate_existing=True)))
    if {attempt.id for _, attempt in rows} != set(attempt_ids):
        raise ValueError("worker_exit_exact_attempt_set_changed")
    return rows


def _require_legacy_resources(session, attempt_ids):
    for model in (AccountBehaviorBudgetReservation, AccountPoolConcurrencyLease, RemoteInvocationFence):
        if session.scalar(select(model.id).where(model.attempt_id.in_(attempt_ids)).limit(1)) is not None:
            raise ValueError("worker_exit_requires_legacy_resource_path")


def _validate_spec(spec):
    tenant_id = spec.get("tenant_id")
    if type(tenant_id) is not int or tenant_id <= 0:
        raise ValueError("worker_exit_tenant_required")
    attempt_ids = _validate_attempt_ids(spec)
    _require_sha(spec.get("deployed_sha"))
    return tenant_id, attempt_ids


def _validate_attempt_ids(spec):
    attempt_ids = spec.get("attempt_ids")
    if not isinstance(attempt_ids, list) or not attempt_ids or any(
            not isinstance(value, str) or not value for value in attempt_ids):
        raise ValueError("worker_exit_exact_attempts_required")
    if (type(spec.get("expected_attempt_count")) is not int
            or len(set(attempt_ids)) != len(attempt_ids)
            or len(attempt_ids) != spec["expected_attempt_count"]):
        raise ValueError("worker_exit_attempt_count_mismatch")
    return tuple(attempt_ids)


def _validate_operation(preview, operation):
    if not operation.actor.strip() or not operation.audit_reference.strip():
        raise ValueError("worker_exit_actor_reference_required")
    if len(operation.actor) > 100 or len(operation.audit_reference) > 100:
        raise ValueError("worker_exit_actor_reference_too_long")
    _require_sha(operation.deployed_sha)
    if (preview.get("schema_version") != ACK_SCHEMA
            or canonical_state_hash(preview.get("state")) != preview.get("state_hash")):
        raise ValueError("worker_exit_preview_invalid")
    if preview["state"]["spec"]["deployed_sha"] != operation.deployed_sha:
        raise ValueError("worker_exit_deployed_sha_changed")


def _prior_receipt(session, preview, operation):
    row = _receipt_audit(session, preview["state"]["spec"]["tenant_id"], preview["state_hash"])
    if row is None:
        return None
    receipt = json.loads(row.detail)
    if receipt["audit_reference"] != operation.audit_reference:
        raise ValueError("worker_exit_audit_reference_conflict")
    return receipt


def _receipt_audit(session, tenant_id, preview_hash):
    return session.scalar(select(AuditLog).where(AuditLog.tenant_id == tenant_id,
        AuditLog.action == ACK_AUDIT, AuditLog.target_type == ACK_TARGET, AuditLog.target_id == preview_hash))


def _stamp(value):
    return as_beijing_aware(value).astimezone(timezone.utc).isoformat() if value is not None else None


def _require_sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("worker_exit_deployed_sha_required")
