"""Read a failed call's persisted result without inferring it from call-start."""
from sqlalchemy import select

from app.models import GatewayRequestEvidenceJournal
from app.timezone import as_beijing

from .engagement_gateway_return import journal_matches_original_call


OUTCOME_UNPROVEN = "engagement_recovery_outcome_unproven"
JOURNAL_FIELDS = {
    "journal_state": "state", "journal_mutation": "remote_mutation_state",
    "journal_request": "gateway_request_identity", "journal_request_hash": "request_fingerprint",
    "journal_target_hash": "target_fingerprint", "journal_result_hash": "result_fingerprint",
    "journal_evidence_hash": "evidence_hash", "journal_message_id": "remote_message_id",
    "journal_fact_id": "remote_fact_id", "journal_typed_fact": "typed_remote_fact",
    "journal_failure_code": "failure_code", "journal_observed_at": "observed_at",
}


def recovered_mutation_state(session, attempt, action):
    if attempt.status != "failed" or attempt.gateway_call_started_at is None:
        return None
    if (attempt.action_id != action.id or attempt.tenant_id != action.tenant_id
            or attempt.account_id != action.account_id
            or attempt.task_lifecycle_epoch != action.task_lifecycle_epoch):
        raise RuntimeError(OUTCOME_UNPROVEN)
    journals = list(session.scalars(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id)))
    if journals:
        states = {_journal_mutation(attempt, item) for item in journals}
        if len(states) != 1:
            raise RuntimeError(OUTCOME_UNPROVEN)
        return states.pop()
    snapshot = attempt.result_snapshot or {}
    observed = snapshot.get("remote_mutation_started")
    if (type(observed) is not bool or attempt.after_call_at is None
            or as_beijing(attempt.after_call_at) < as_beijing(attempt.gateway_call_started_at)
            or (observed is False and attempt.remote_message_id)):
        raise RuntimeError(OUTCOME_UNPROVEN)
    return observed


def _journal_mutation(attempt, journal):
    if (journal.tenant_id != attempt.tenant_id or journal.action_id != attempt.action_id
            or journal.account_id != attempt.account_id
            or journal.remote_mutation_state not in {"true", "false"}):
        raise RuntimeError(OUTCOME_UNPROVEN)
    if journal.remote_mutation_state == "false" and (journal.remote_message_id or journal.remote_fact_id):
        raise RuntimeError(OUTCOME_UNPROVEN)
    snapshot = attempt.result_snapshot or {}
    row = {"call_at": attempt.gateway_call_started_at,
        "attempt_request": snapshot.get("gateway_request_identity"),
        "attempt_request_hash": snapshot.get("gateway_request_fingerprint"),
        "attempt_target_hash": snapshot.get("gateway_target_fingerprint")}
    proof = {key: getattr(journal, field) for key, field in JOURNAL_FIELDS.items()}
    if not journal_matches_original_call(row, proof):
        raise RuntimeError(OUTCOME_UNPROVEN)
    return journal.remote_mutation_state == "true"
