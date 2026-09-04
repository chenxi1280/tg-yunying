"""Operator-reviewed recovery; no synthetic message or visibility evidence."""
import json

from sqlalchemy import select

from app.models import NegativeOutcomeCircuitState
from app.services._common import _now, audit
from .negative_outcome_circuit import _lock_tenant


def circuit_snapshot(circuit):
    return {key: getattr(circuit, key) for key in (
        "id", "tenant_id", "route", "peer_id", "account_id", "level", "version",
        "reason", "events", "entered_at", "eligible_exit_at", "updated_at",
    )}


def review_negative_outcome(session, circuit_id, *, tenant_id, expected_version, reason, evidence, actor):
    if not reason.strip() or not evidence.strip():
        raise ValueError("negative_outcome_review_evidence_required")
    _lock_tenant(session, tenant_id)
    circuit = session.scalar(select(NegativeOutcomeCircuitState).where(
        NegativeOutcomeCircuitState.id == circuit_id, NegativeOutcomeCircuitState.tenant_id == tenant_id,
    ).with_for_update().execution_options(populate_existing=True))
    if circuit is None:
        raise LookupError("negative_outcome_circuit_not_found")
    if circuit.version != expected_version:
        raise ValueError("negative_outcome_review_version_conflict")
    if circuit.level == "normal":
        raise ValueError("negative_outcome_review_not_blocked")
    before = {"level": circuit.level, "version": circuit.version}
    circuit.events = [{**event, "reviewed": True} for event in circuit.events or []]
    circuit.level = "normal"
    circuit.eligible_exit_at = None
    circuit.reason = "recovered_by_operator_review"
    circuit.version += 1
    circuit.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="negative_outcome_review",
        target_type="negative_outcome_circuit", target_id=circuit.id,
        detail=json.dumps({"before": before, "reason": reason.strip(), "evidence": evidence.strip(),
                           "route": circuit.route, "peer_id": circuit.peer_id, "account_id": circuit.account_id},
                          ensure_ascii=False))
    session.flush()
    return circuit_snapshot(circuit)
