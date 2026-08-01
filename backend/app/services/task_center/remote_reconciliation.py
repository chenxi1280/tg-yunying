from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    RemoteReconcileCase,
)
from app.services._common import _now

from .dispatch_claim_ledger import for_update, lock_dispatch_claim_prefix
from .remote_reconcile_business_facts import (
    apply_confirmed_business_fact,
    typed_remote_fact_id,
)
from .runtime_state_hash import (
    canonical_state_hash,
    execution_attempt_state_hash,
    remote_reconcile_action_state_hash,
)


REMOTE_RESULTS = frozenset(
    {"remote_confirmed", "remote_absence_proven", "inconclusive"}
)
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
@dataclass(frozen=True)
class RemoteReconcileEvidence:
    result: str
    source: str
    evidence_fingerprint: str
    remote_message_id: str = ""
    remote_fact_id: str = ""
    failure_code: str = ""
    remote_mutation_started: bool | None = None
    exact_match_count: int = 0


@dataclass(frozen=True)
class RemoteReconcileOutcome:
    case_id: str
    state: str
    changed: bool
    evidence_hash: str


def ensure_remote_reconcile_case(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
) -> RemoteReconcileCase:
    existing = session.scalar(select(RemoteReconcileCase).where(
        RemoteReconcileCase.action_id == action.id,
        RemoteReconcileCase.execution_attempt_id == attempt.id,
    ))
    if existing is not None:
        return existing
    row = RemoteReconcileCase(
        action_id=action.id,
        execution_attempt_id=attempt.id,
        expected_action_state_hash=remote_reconcile_action_state_hash(action),
        expected_attempt_state_hash=execution_attempt_state_hash(attempt),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        existing = session.scalar(select(RemoteReconcileCase).where(
            RemoteReconcileCase.action_id == action.id,
            RemoteReconcileCase.execution_attempt_id == attempt.id,
        ))
        if existing is not None:
            return existing
        raise


def apply_remote_reconcile_evidence(
    session: Session,
    case_id: str,
    evidence: RemoteReconcileEvidence,
    *,
    actor: str,
    checked_at: datetime | None = None,
) -> RemoteReconcileOutcome:
    observed_at = checked_at or _now()
    _validate_evidence(evidence)
    evidence_hash = _evidence_hash(evidence)
    hint = session.get(RemoteReconcileCase, case_id)
    if hint is None:
        raise ValueError("remote_reconcile_case_not_found")
    action_hint = session.get(Action, hint.action_id)
    if action_hint is None:
        raise RuntimeError("remote_reconcile_action_missing")
    with session.no_autoflush:
        lock_dispatch_claim_prefix(session, action_hint)
        action = _locked(session, Action, hint.action_id)
        attempt = _locked(session, ExecutionAttempt, hint.execution_attempt_id)
        case = _locked(session, RemoteReconcileCase, case_id)
    if action is None or attempt is None or case is None:
        raise RuntimeError("remote_reconcile_fact_incomplete")
    replay = _replayed_outcome(case, evidence.result, evidence_hash)
    if replay is not None:
        return replay
    if case.state not in {"pending", "inconclusive"}:
        return _mark_conflict(
            session, case, action, evidence_hash, actor, observed_at,
        )
    if not _state_hashes_match(case, action, attempt):
        return _mark_conflict(
            session, case, action, evidence_hash, actor, observed_at,
        )
    _apply_remote_result(action, attempt, evidence, observed_at)
    apply_confirmed_business_fact(
        session,
        action,
        attempt,
        result=evidence.result,
        remote_fact_id=evidence.remote_fact_id,
    )
    _finalize_business_state(session, action)
    _write_case_evidence(case, evidence, evidence_hash, actor, observed_at)
    if evidence.result == "inconclusive":
        case.expected_action_state_hash = remote_reconcile_action_state_hash(
            action
        )
        case.expected_attempt_state_hash = execution_attempt_state_hash(attempt)
    _write_audit(session, case, action, evidence)
    return RemoteReconcileOutcome(case.id, case.state, True, evidence_hash)


def evidence_from_gateway_journal(
    session: Session,
    case_id: str,
) -> RemoteReconcileEvidence:
    case = session.get(RemoteReconcileCase, case_id)
    if case is None:
        raise ValueError("remote_reconcile_case_not_found")
    attempt = session.get(ExecutionAttempt, case.execution_attempt_id)
    action = session.get(Action, case.action_id)
    if attempt is None or action is None:
        raise RuntimeError("remote_reconcile_fact_incomplete")
    identity = str(
        (attempt.result_snapshot or {}).get("gateway_request_identity")
        or (action.result or {}).get("gateway_request_identity")
        or ""
    )
    journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.gateway_request_identity == identity,
    )) if identity else None
    if journal is None or not _journal_identity_matches(journal, action, attempt):
        return _inconclusive_journal_evidence(case, "gateway_journal_missing")
    if journal.state != "recorded":
        return _inconclusive_journal_evidence(case, "gateway_journal_conflict")
    if journal.remote_mutation_state == "true" and (
        journal.remote_message_id or journal.remote_fact_id
    ):
        return RemoteReconcileEvidence(
            result="remote_confirmed",
            source="gateway_request_evidence_journal",
            evidence_fingerprint=journal.evidence_hash,
            remote_message_id=journal.remote_message_id,
            remote_fact_id=journal.remote_fact_id,
            remote_mutation_started=True,
            exact_match_count=1,
        )
    if journal.remote_mutation_state == "false":
        return RemoteReconcileEvidence(
            result="remote_absence_proven",
            source="gateway_request_evidence_journal",
            evidence_fingerprint=journal.evidence_hash,
            failure_code=journal.failure_code or "remote_mutation_not_started",
            remote_mutation_started=False,
        )
    return _inconclusive_journal_evidence(case, "gateway_journal_inconclusive")


def _journal_identity_matches(
    journal: GatewayRequestEvidenceJournal,
    action: Action,
    attempt: ExecutionAttempt,
) -> bool:
    return bool(
        journal.action_id == action.id
        and journal.execution_attempt_id == attempt.id
        and journal.account_id == attempt.account_id
    )


def _inconclusive_journal_evidence(
    case: RemoteReconcileCase,
    source: str,
) -> RemoteReconcileEvidence:
    return RemoteReconcileEvidence(
        result="inconclusive",
        source=source,
        evidence_fingerprint=canonical_state_hash({
            "case_id": case.id,
            "source": source,
        }),
    )


def _validate_evidence(evidence: RemoteReconcileEvidence) -> None:
    if evidence.result not in REMOTE_RESULTS:
        raise ValueError("remote_reconcile_result_invalid")
    if not evidence.source.strip() or not HEX_64.fullmatch(
        evidence.evidence_fingerprint,
    ):
        raise ValueError("remote_reconcile_evidence_invalid")
    if evidence.result == "remote_confirmed" and not (
        evidence.exact_match_count == 1
        and (evidence.remote_message_id.strip() or evidence.remote_fact_id.strip())
    ):
        raise ValueError("remote_reconcile_exact_fact_required")
    if (
        evidence.result == "remote_absence_proven"
        and evidence.remote_mutation_started is not False
    ):
        raise ValueError("remote_reconcile_authoritative_absence_required")


def _evidence_hash(evidence: RemoteReconcileEvidence) -> str:
    return canonical_state_hash({
        "result": evidence.result,
        "source": evidence.source,
        "evidence_fingerprint": evidence.evidence_fingerprint,
        "remote_message_id": evidence.remote_message_id,
        "remote_fact_id": evidence.remote_fact_id,
        "failure_code": evidence.failure_code,
        "remote_mutation_started": evidence.remote_mutation_started,
        "exact_match_count": evidence.exact_match_count,
    })


def remote_reconcile_evidence_hash(
    evidence: RemoteReconcileEvidence,
) -> str:
    return _evidence_hash(evidence)


def _locked(session: Session, model, row_id: str):
    statement = select(model).where(model.id == row_id)
    return session.scalar(for_update(session, statement))


def _replayed_outcome(
    case: RemoteReconcileCase,
    result: str,
    evidence_hash: str,
) -> RemoteReconcileOutcome | None:
    if case.state == result and case.evidence_hash == evidence_hash:
        return RemoteReconcileOutcome(case.id, case.state, False, evidence_hash)
    return None


def _state_hashes_match(
    case: RemoteReconcileCase,
    action: Action,
    attempt: ExecutionAttempt,
) -> bool:
    return bool(
        case.expected_action_state_hash
        == remote_reconcile_action_state_hash(action)
        and case.expected_attempt_state_hash
        == execution_attempt_state_hash(attempt)
    )


def _mark_conflict(
    session: Session,
    case: RemoteReconcileCase,
    action: Action,
    evidence_hash: str,
    actor: str,
    observed_at: datetime,
) -> RemoteReconcileOutcome:
    case.state = "conflict"
    case.evidence_hash = evidence_hash
    case.actor = actor
    case.checked_at = observed_at
    _write_conflict_audit(session, case, action)
    return RemoteReconcileOutcome(case.id, case.state, True, evidence_hash)


def _write_conflict_audit(
    session: Session,
    case: RemoteReconcileCase,
    action: Action,
) -> None:
    session.add(AuditLog(
        tenant_id=action.tenant_id,
        actor=case.actor[:100],
        action="远端发送核验冲突隔离",
        target_type="remote_reconcile_case",
        target_id=case.id,
        detail=json.dumps({
            "result": "conflict",
            "evidence_hash": case.evidence_hash,
            "reason": "state_or_evidence_conflict",
        }, ensure_ascii=False, sort_keys=True),
    ))


def _apply_remote_result(
    action: Action,
    attempt: ExecutionAttempt,
    evidence: RemoteReconcileEvidence,
    observed_at: datetime,
) -> None:
    if evidence.result == "remote_confirmed":
        _apply_confirmed(action, attempt, evidence, observed_at)
        return
    if evidence.result == "remote_absence_proven":
        _apply_absence(action, attempt, evidence, observed_at)
        return
    _apply_inconclusive(action, attempt, observed_at)


def _apply_confirmed(
    action: Action,
    attempt: ExecutionAttempt,
    evidence: RemoteReconcileEvidence,
    observed_at: datetime,
) -> None:
    attempt.status = "success"
    attempt.after_call_at = attempt.after_call_at or observed_at
    attempt.remote_message_id = evidence.remote_message_id
    attempt.failure_type = ""
    attempt.failure_detail = ""
    attempt.result_snapshot = {
        **dict(attempt.result_snapshot or {}),
        "success": True,
        "remote_message_id": evidence.remote_message_id,
        "remote_fact_id": evidence.remote_fact_id,
        "remote_reconcile_result": "remote_confirmed",
    }
    action.status = "success"
    action.executed_at = action.executed_at or observed_at
    action.result = {
        **dict(action.result or {}),
        "success": True,
        "remote_message_id": evidence.remote_message_id,
        "remote_fact_id": evidence.remote_fact_id,
        "remote_reconcile_result": "remote_confirmed",
        "error_code": "",
    }


def _apply_absence(
    action: Action,
    attempt: ExecutionAttempt,
    evidence: RemoteReconcileEvidence,
    observed_at: datetime,
) -> None:
    code = evidence.failure_code or "remote_mutation_not_started"
    attempt.status = "failed"
    attempt.after_call_at = attempt.after_call_at or observed_at
    attempt.failure_type = code
    attempt.result_snapshot = {
        **dict(attempt.result_snapshot or {}),
        "success": False,
        "error_code": code,
        "remote_reconcile_result": "remote_absence_proven",
    }
    action.status = "failed"
    action.executed_at = observed_at
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": code,
        "remote_reconcile_result": "remote_absence_proven",
        "fulfillment_replan_required": True,
    }


def _apply_inconclusive(
    action: Action,
    attempt: ExecutionAttempt,
    observed_at: datetime,
) -> None:
    attempt.status = "result_unknown"
    attempt.after_call_at = attempt.after_call_at or observed_at
    attempt.failure_type = "remote_reconcile_inconclusive"
    attempt.result_snapshot = {
        **dict(attempt.result_snapshot or {}),
        "remote_reconcile_result": "inconclusive",
    }
    action.status = "unknown_after_send"
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "remote_reconcile_inconclusive",
        "remote_reconcile_result": "inconclusive",
    }


def _finalize_business_state(session: Session, action: Action) -> None:
    from .dispatcher import _finalize_dispatch_action

    _finalize_dispatch_action(
        session,
        action,
        ensure_remote_case=False,
        project_task_stats=False,
    )


def _write_case_evidence(
    case: RemoteReconcileCase,
    evidence: RemoteReconcileEvidence,
    evidence_hash: str,
    actor: str,
    observed_at: datetime,
) -> None:
    case.state = evidence.result
    case.evidence_hash = evidence_hash
    case.actor = actor
    case.evidence_source = evidence.source
    case.evidence_fingerprint = evidence.evidence_fingerprint
    case.remote_message_id = evidence.remote_message_id
    case.remote_fact_id = evidence.remote_fact_id
    case.failure_code = evidence.failure_code
    case.checked_at = observed_at


def _write_audit(
    session: Session,
    case: RemoteReconcileCase,
    action: Action,
    evidence: RemoteReconcileEvidence,
) -> None:
    session.add(AuditLog(
        tenant_id=action.tenant_id,
        actor=case.actor[:100],
        action="远端发送结果核验",
        target_type="remote_reconcile_case",
        target_id=case.id,
        detail=json.dumps({
            "result": evidence.result,
            "source": evidence.source,
            "evidence_fingerprint": evidence.evidence_fingerprint,
            "remote_message_id": evidence.remote_message_id,
            "remote_fact_id": evidence.remote_fact_id,
            "failure_code": evidence.failure_code,
        }, ensure_ascii=False, sort_keys=True),
    ))


__all__ = [
    "RemoteReconcileEvidence",
    "RemoteReconcileOutcome",
    "apply_remote_reconcile_evidence",
    "evidence_from_gateway_journal",
    "ensure_remote_reconcile_case",
    "remote_reconcile_evidence_hash",
    "typed_remote_fact_id",
]
