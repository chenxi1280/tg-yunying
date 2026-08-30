from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, RemoteReconcileCase, TgAccountAuthorization
from app.models.group_clone import TelegramGatewayMutationIdentity
from app.services.developer_apps import credentials_for_authorization

from .payloads import GroupCloneMutationPayload
from .recovery_claims import RecoveryClaim, recovery_claim_owned, release_recovery_claim
from .remote_reconciliation import (
    RemoteReconcileEvidence,
    apply_remote_reconcile_evidence,
)
from .runtime_state_hash import (
    canonical_state_hash,
    execution_attempt_state_hash,
    remote_reconcile_action_state_hash,
)

PROBE_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class DesiredStateProbe:
    matched: bool
    target_id: str
    observed: dict


def recover_unknown_clone_mutation(
    session: Session,
    claim: RecoveryClaim,
    *,
    gateway,
    now: datetime,
) -> int:
    action = session.get(Action, claim.action_id)
    if not recovery_claim_owned(action, claim):
        session.rollback()
        return 0
    if not gateway.supports_group_clone_desired_state_probe:
        release_recovery_claim(action, claim)
        session.commit()
        return 0
    attempt, case = _runtime_rows(session, action)
    release_recovery_claim(action, claim)
    action.result = {
        **dict(action.result or {}),
        "group_clone_probe_next_at": (
            now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
        ).isoformat(),
    }
    _refresh_case_hashes(case, action, attempt)
    probe = _probe(session, action, gateway=gateway)
    evidence = _evidence(action, attempt, probe)
    outcome = apply_remote_reconcile_evidence(
        session,
        case.id,
        evidence,
        actor="group-clone-desired-state-reconcile",
        checked_at=now,
    )
    session.commit()
    return int(outcome.state == "remote_confirmed")


def _runtime_rows(session: Session, action: Action):
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))
    case_id = str(dict(action.result or {}).get("remote_reconcile_case_id") or "")
    case = session.get(RemoteReconcileCase, case_id) if case_id else None
    if attempt is None or case is None:
        raise RuntimeError("group_clone_remote_reconcile_runtime_missing")
    return attempt, case


def _refresh_case_hashes(case, action, attempt) -> None:
    case.expected_action_state_hash = remote_reconcile_action_state_hash(action)
    case.expected_attempt_state_hash = execution_attempt_state_hash(attempt)


def _probe(session: Session, action: Action, *, gateway) -> DesiredStateProbe:
    payload = GroupCloneMutationPayload.model_validate(action.payload or {})
    identity = session.get(
        TelegramGatewayMutationIdentity,
        payload.gateway_mutation_identity_id,
    )
    authorization = session.get(
        TgAccountAuthorization,
        identity.authorization_id if identity else 0,
    )
    if identity is None or authorization is None:
        raise RuntimeError("group_clone_remote_reconcile_identity_missing")
    credentials = credentials_for_authorization(session, authorization)
    common = {
        "session_ciphertext": authorization.session_ciphertext,
        "credentials": credentials,
    }
    return _probe_kind(
        gateway,
        payload,
        account_id=identity.account_id,
        common=common,
    )


def _probe_kind(gateway, payload, *, account_id: int, common: dict) -> DesiredStateProbe:
    target_id = payload.target_message_ids[0] if payload.target_message_ids else 0
    if payload.mutation_kind in {"editMessage", "deleteMessages"}:
        message = gateway.fetch_group_message(
            account_id,
            payload.target_peer_id,
            str(target_id),
            **common,
        )
        observed = {"exists": message is not None, "content": message.content if message else ""}
        matched = message is None if payload.mutation_kind == "deleteMessages" else bool(
            message is not None and message.content == payload.content
        )
        return DesiredStateProbe(matched, str(target_id), observed)
    if payload.mutation_kind in {"pinMessage", "unpinMessage"}:
        pinned_id = gateway.fetch_raw_pinned_message_id(payload.target_peer_id, **common)
        matched = pinned_id == target_id if payload.mutation_kind == "pinMessage" else pinned_id is None
        return DesiredStateProbe(matched, str(target_id), {"pinned_message_id": pinned_id})
    return _probe_topic(gateway, payload, target_id=target_id, common=common)


def _probe_topic(gateway, payload, *, target_id: int, common: dict) -> DesiredStateProbe:
    if payload.mutation_kind == "createForumTopic":
        return DesiredStateProbe(False, "", {"reason": "random_id_update_mapping_required"})
    try:
        topic = gateway.fetch_raw_forum_topic(payload.target_peer_id, target_id, **common)
    except RuntimeError as exc:
        if str(exc) != "source_forum_topic_not_found":
            raise
        topic = None
    if payload.mutation_kind == "deleteForumTopic":
        return DesiredStateProbe(topic is None, str(target_id), {"exists": topic is not None})
    matched = bool(topic and _topic_matches(payload, topic))
    return DesiredStateProbe(matched, str(target_id), dict(topic or {"exists": False}))


def _topic_matches(payload, topic: dict) -> bool:
    expected = {
        "title": payload.content or None,
        "closed": payload.topic_closed,
        "hidden": payload.topic_hidden,
        "icon_emoji_id": (
            str(payload.topic_icon_emoji_id) if payload.topic_icon_emoji_id is not None else None
        ),
    }
    return all(
        value is None or str(topic.get(key)) == str(value)
        for key, value in expected.items()
    )


def _evidence(action, attempt, probe: DesiredStateProbe) -> RemoteReconcileEvidence:
    observed = {
        "action_id": action.id,
        "attempt_id": attempt.id,
        "matched": probe.matched,
        "target_id": probe.target_id,
        "observed": probe.observed,
    }
    return RemoteReconcileEvidence(
        result="remote_confirmed" if probe.matched else "inconclusive",
        source="group_clone_desired_state_readback",
        evidence_fingerprint=canonical_state_hash(observed),
        remote_message_id=probe.target_id if probe.matched else "",
        remote_mutation_started=True if probe.matched else None,
        exact_match_count=1 if probe.matched else 0,
        failure_code="" if probe.matched else "desired_state_not_observed",
    )


__all__ = ["recover_unknown_clone_mutation"]
