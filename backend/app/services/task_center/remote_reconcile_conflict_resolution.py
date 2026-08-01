from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AuditLog, ExecutionAttempt, RemoteReconcileCase
from app.services._common import _now

from .dispatch_claim_ledger import for_update, lock_dispatch_claim_prefix
from .remote_reconciliation import (
    RemoteReconcileEvidence,
    RemoteReconcileOutcome,
    apply_remote_reconcile_evidence,
    remote_reconcile_evidence_hash,
)
from .runtime_state_hash import (
    execution_attempt_state_hash,
    remote_reconcile_action_state_hash,
)


def resolve_remote_reconcile_conflict(
    session: Session,
    case_id: str,
    evidence: RemoteReconcileEvidence,
    *,
    expected_action_state_hash: str,
    expected_attempt_state_hash: str,
    actor: str,
    checked_at: datetime | None = None,
) -> RemoteReconcileOutcome:
    if evidence.result not in {"remote_confirmed", "remote_absence_proven"}:
        raise ValueError("remote_reconcile_conflict_terminal_evidence_required")
    case, action, attempt = _locked_case_facts(session, case_id)
    if action.status != "unknown_after_send" or attempt.status != "result_unknown":
        raise ValueError("remote_reconcile_conflict_unknown_state_required")
    current_action_hash = remote_reconcile_action_state_hash(action)
    current_attempt_hash = execution_attempt_state_hash(attempt)
    _validate_resolution(
        case,
        evidence_hash=remote_reconcile_evidence_hash(evidence),
        expected_action_hash=expected_action_state_hash,
        expected_attempt_hash=expected_attempt_state_hash,
        current_action_hash=current_action_hash,
        current_attempt_hash=current_attempt_hash,
    )
    _write_resolution_audit(
        session,
        case,
        action,
        actor=actor,
        action_hash=current_action_hash,
        attempt_hash=current_attempt_hash,
    )
    case.state = "pending"
    case.expected_action_state_hash = current_action_hash
    case.expected_attempt_state_hash = current_attempt_hash
    return apply_remote_reconcile_evidence(
        session, case.id, evidence, actor=actor, checked_at=checked_at or _now(),
    )


def _locked_case_facts(
    session: Session,
    case_id: str,
) -> tuple[RemoteReconcileCase, Action, ExecutionAttempt]:
    hint = session.get(RemoteReconcileCase, case_id)
    if hint is None:
        raise ValueError("remote_reconcile_case_not_found")
    action_hint = session.get(Action, hint.action_id)
    if action_hint is None:
        raise RuntimeError("remote_reconcile_action_missing")
    lock_dispatch_claim_prefix(session, action_hint)
    action = _locked(session, Action, hint.action_id)
    attempt = _locked(session, ExecutionAttempt, hint.execution_attempt_id)
    case = _locked(session, RemoteReconcileCase, case_id)
    if action is None or attempt is None or case is None:
        raise RuntimeError("remote_reconcile_fact_incomplete")
    return case, action, attempt


def _validate_resolution(
    case: RemoteReconcileCase,
    *,
    evidence_hash: str,
    expected_action_hash: str,
    expected_attempt_hash: str,
    current_action_hash: str,
    current_attempt_hash: str,
) -> None:
    if case.state != "conflict":
        raise ValueError("remote_reconcile_case_not_conflict")
    if case.evidence_hash != evidence_hash:
        raise ValueError("remote_reconcile_conflict_evidence_mismatch")
    if expected_action_hash != current_action_hash:
        raise ValueError("remote_reconcile_current_action_hash_mismatch")
    if expected_attempt_hash != current_attempt_hash:
        raise ValueError("remote_reconcile_current_attempt_hash_mismatch")


def _write_resolution_audit(
    session: Session,
    case: RemoteReconcileCase,
    action: Action,
    *,
    actor: str,
    action_hash: str,
    attempt_hash: str,
) -> None:
    session.add(AuditLog(
        tenant_id=action.tenant_id,
        actor=actor[:100],
        action="远端发送核验冲突复核",
        target_type="remote_reconcile_case",
        target_id=case.id,
        detail=json.dumps({
            "previous_expected_action_state_hash": case.expected_action_state_hash,
            "previous_expected_attempt_state_hash": case.expected_attempt_state_hash,
            "accepted_action_state_hash": action_hash,
            "accepted_attempt_state_hash": attempt_hash,
        }, ensure_ascii=False, sort_keys=True),
    ))


def _locked(session: Session, model, row_id: str):
    return session.scalar(for_update(session, select(model).where(model.id == row_id)))


__all__ = ["resolve_remote_reconcile_conflict"]
