"""Separate original membership transport completion from unknown business effects."""
from sqlalchemy import and_
from sqlalchemy.orm import load_only

from app.models import Action, ExecutionAttempt, GatewayRequestEvidenceJournal, OperationTarget

from .engagement_gateway_return import journal_matches_original_call


UNKNOWN_ATTEMPT_STATES = ("result_unknown", "unknown_after_send", "remote_unknown")
REQUEST_FIELDS = ("gateway_request_identity", "gateway_request_fingerprint", "gateway_target_fingerprint")


def membership_reprobe_snapshot(attempt, action_result):
    frozen = dict(attempt.result_snapshot or {})
    merged = {**frozen, **dict(action_result or {})}
    protected = {key: value for key, value in frozen.items()
        if key in REQUEST_FIELDS or key.startswith("transport_termination_")}
    return {**merged, **protected}


def membership_attempt_rows(session, query):
    journal = GatewayRequestEvidenceJournal
    return session.execute(query.with_only_columns(Action, ExecutionAttempt, journal, OperationTarget)
        .outerjoin(journal, journal.execution_attempt_id == ExecutionAttempt.id)
        .outerjoin(OperationTarget, and_(
            OperationTarget.id == Action.payload["channel_target_id"].as_integer(),
            OperationTarget.tenant_id == Action.tenant_id))
        .options(load_only(Action.id, Action.tenant_id, Action.account_id, Action.task_lifecycle_epoch),
            load_only(OperationTarget.id, OperationTarget.tg_peer_id)))


def membership_transport_ended(action, attempt, journal):
    if not _attempt_owner_matches(action, attempt):
        return False
    snapshot = attempt.result_snapshot or {}
    termination = snapshot.get("transport_termination_state")
    if termination == "acknowledged":
        return True
    if termination or journal is None:
        return False
    if not _journal_owner_matches(attempt, journal):
        return False
    if snapshot.get("gateway_request_identity") != f"telegram-gateway:{attempt.id}":
        return False
    original = {"call_at": attempt.gateway_call_started_at,
        "attempt_request": snapshot.get("gateway_request_identity"),
        "attempt_request_hash": snapshot.get("gateway_request_fingerprint"),
        "attempt_target_hash": snapshot.get("gateway_target_fingerprint")}
    return journal_matches_original_call(original, _journal_proof(journal))


def unknown_membership_blocks_target(attempt, original_target, target):
    if attempt.status not in UNKNOWN_ATTEMPT_STATES:
        return False
    if original_target is None or not original_target.tg_peer_id:
        return True
    return original_target.id == target.id or original_target.tg_peer_id == target.tg_peer_id


def _attempt_owner_matches(action, attempt):
    return (action.id == attempt.action_id and action.tenant_id == attempt.tenant_id
        and action.account_id == attempt.account_id
        and action.task_lifecycle_epoch == attempt.task_lifecycle_epoch)


def _journal_owner_matches(attempt, journal):
    return (journal.tenant_id == attempt.tenant_id and journal.action_id == attempt.action_id
        and journal.execution_attempt_id == attempt.id and journal.account_id == attempt.account_id)


def _journal_proof(journal):
    return {"journal_state": journal.state, "journal_mutation": journal.remote_mutation_state,
        "journal_observed_at": journal.observed_at,
        "journal_request": journal.gateway_request_identity,
        "journal_request_hash": journal.request_fingerprint,
        "journal_target_hash": journal.target_fingerprint,
        "journal_message_id": journal.remote_message_id,
        "journal_fact_id": journal.remote_fact_id,
        "journal_typed_fact": journal.typed_remote_fact,
        "journal_failure_code": journal.failure_code,
        "journal_result_hash": journal.result_fingerprint,
        "journal_evidence_hash": journal.evidence_hash}
