from __future__ import annotations

from dataclasses import dataclass
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, object_session

from app.models import Action, ExecutionAttempt, GatewayRequestEvidenceJournal
from app.services._common import _now

from .runtime_state_hash import canonical_state_hash


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayResultEvidence:
    remote_message_id: str = ""
    remote_fact_id: str = ""
    failure_code: str = ""
    remote_mutation_started: bool | None = None


def gateway_request_identity(attempt: ExecutionAttempt) -> str:
    return f"telegram-gateway:{attempt.id}"


def bind_gateway_request_identity(
    action: Action,
    attempt: ExecutionAttempt,
) -> str:
    identity = gateway_request_identity(attempt)
    request_fingerprint = canonical_state_hash(_request_fact(action))
    target_fingerprint = canonical_state_hash(_target_fact(action))
    frozen = {
        "gateway_request_identity": identity,
        "gateway_request_fingerprint": request_fingerprint,
        "gateway_target_fingerprint": target_fingerprint,
    }
    action.result = {
        **dict(action.result or {}),
        **frozen,
    }
    attempt.result_snapshot = {
        **dict(attempt.result_snapshot or {}),
        **frozen,
    }
    return identity


def persist_gateway_result_evidence(
    action: Action,
    attempt: ExecutionAttempt,
    evidence: GatewayResultEvidence,
) -> bool:
    source_session = object_session(action)
    if source_session is None:
        raise RuntimeError("gateway_evidence_action_detached")
    bind = source_session.get_bind()
    if bind.dialect.name == "sqlite":
        record_gateway_result_evidence(source_session, action, attempt, evidence)
        return True
    try:
        with Session(bind=bind) as journal_session:
            _record_detached_evidence(journal_session, action, attempt, evidence)
            journal_session.commit()
        return True
    except (SQLAlchemyError, RuntimeError, ValueError):
        logger.exception(
            "gateway evidence journal failed action_id=%s attempt_id=%s",
            action.id,
            attempt.id,
        )
        return False


def record_gateway_result_evidence(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
    evidence: GatewayResultEvidence,
) -> GatewayRequestEvidenceJournal:
    snapshot = _journal_snapshot(action, attempt, evidence)
    existing = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.gateway_request_identity
        == snapshot["gateway_request_identity"],
    ))
    if existing is not None:
        return _reconcile_existing(existing, snapshot)
    row = GatewayRequestEvidenceJournal(**snapshot)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        existing = session.scalar(select(GatewayRequestEvidenceJournal).where(
            GatewayRequestEvidenceJournal.gateway_request_identity
            == snapshot["gateway_request_identity"],
        ))
        if existing is None:
            raise
        return _reconcile_existing(existing, snapshot)


def _record_detached_evidence(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
    evidence: GatewayResultEvidence,
) -> None:
    snapshot = _journal_snapshot(action, attempt, evidence)
    existing = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.gateway_request_identity
        == snapshot["gateway_request_identity"],
    ))
    if existing is None:
        session.add(GatewayRequestEvidenceJournal(**snapshot))
        return
    _reconcile_existing(existing, snapshot)


def _journal_snapshot(
    action: Action,
    attempt: ExecutionAttempt,
    evidence: GatewayResultEvidence,
) -> dict:
    _validate_result_evidence(evidence)
    request_identity = str(
        (attempt.result_snapshot or {}).get("gateway_request_identity")
        or (action.result or {}).get("gateway_request_identity")
        or ""
    )
    if not request_identity:
        raise RuntimeError("gateway_request_identity_missing")
    request_fingerprint, target_fingerprint, drifted = _frozen_fingerprints(
        action, attempt,
    )
    result_fact = _result_fact(evidence)
    evidence_hash = canonical_state_hash({
        "gateway_request_identity": request_identity,
        "request_fingerprint": request_fingerprint,
        "result": result_fact,
    })
    return {
        "tenant_id": action.tenant_id,
        "action_id": action.id,
        "execution_attempt_id": attempt.id,
        "account_id": attempt.account_id,
        "gateway_request_identity": request_identity,
        "request_fingerprint": request_fingerprint,
        "target_fingerprint": target_fingerprint,
        "result_fingerprint": canonical_state_hash(result_fact),
        "evidence_hash": evidence_hash,
        "remote_message_id": evidence.remote_message_id,
        "remote_fact_id": evidence.remote_fact_id,
        "failure_code": evidence.failure_code,
        "remote_mutation_state": _mutation_state(evidence),
        "state": "conflict" if drifted else "recorded",
        "observed_at": _now(),
    }


def _validate_result_evidence(evidence: GatewayResultEvidence) -> None:
    if evidence.remote_mutation_started is False and (
        evidence.remote_message_id or evidence.remote_fact_id
    ):
        raise ValueError("gateway_result_evidence_contradictory")


def _frozen_fingerprints(
    action: Action,
    attempt: ExecutionAttempt,
) -> tuple[str, str, bool]:
    attempt_snapshot = attempt.result_snapshot or {}
    action_result = action.result or {}
    request_fingerprint = str(
        attempt_snapshot.get("gateway_request_fingerprint")
        or action_result.get("gateway_request_fingerprint")
        or ""
    )
    target_fingerprint = str(
        attempt_snapshot.get("gateway_target_fingerprint")
        or action_result.get("gateway_target_fingerprint")
        or ""
    )
    if not request_fingerprint or not target_fingerprint:
        raise RuntimeError("gateway_request_fingerprint_missing")
    current_request = canonical_state_hash(_request_fact(action))
    current_target = canonical_state_hash(_target_fact(action))
    return (
        request_fingerprint,
        target_fingerprint,
        request_fingerprint != current_request
        or target_fingerprint != current_target,
    )


def _reconcile_existing(
    existing: GatewayRequestEvidenceJournal,
    snapshot: dict,
) -> GatewayRequestEvidenceJournal:
    if snapshot["state"] == "conflict":
        existing.state = "conflict"
        return existing
    if existing.evidence_hash == snapshot["evidence_hash"]:
        return existing
    existing.state = "conflict"
    return existing


def _request_fact(action: Action) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return {
        "action_id": action.id,
        "action_type": action.action_type,
        "payload_fingerprint": canonical_state_hash(payload),
    }


def _target_fact(action: Action) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    keys = (
        "group_id", "chat_id", "channel_id", "channel_target_id",
        "operation_target_id", "target_operation_target_id",
        "target_peer_type", "target_peer_id", "target_top_message_id",
        "message_id", "channel_message_id", "source_message_id",
        "remote_message_id", "reaction_emoji",
        "gateway_mutation_identity_id", "random_id",
        "route_snapshot_id", "execution_snapshot_id",
    )
    return {
        "tenant_id": action.tenant_id,
        "account_id": action.account_id,
        "target": {key: payload[key] for key in keys if key in payload},
    }


def _result_fact(evidence: GatewayResultEvidence) -> dict:
    return {
        "remote_message_id": evidence.remote_message_id,
        "remote_fact_id": evidence.remote_fact_id,
        "failure_code": evidence.failure_code,
        "remote_mutation_state": _mutation_state(evidence),
    }


def _mutation_state(evidence: GatewayResultEvidence) -> str:
    if (
        evidence.remote_mutation_started is True
        or evidence.remote_message_id
        or evidence.remote_fact_id
    ):
        return "true"
    if evidence.remote_mutation_started is False:
        return "false"
    return "unknown"


__all__ = [
    "GatewayResultEvidence",
    "bind_gateway_request_identity",
    "persist_gateway_result_evidence",
    "record_gateway_result_evidence",
]
