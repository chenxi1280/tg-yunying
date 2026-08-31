from __future__ import annotations

from sqlalchemy import select

from app.models import Action, ExecutionAttempt, RemoteReconcileCase
from app.models.telegram_updates import TelegramOutboundRandomIdMapping

from .remote_reconciliation import (
    RemoteReconcileEvidence,
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
)
from .runtime_state_hash import canonical_state_hash
from .payloads import GroupCloneSendPayload


def reconcile_update_mappings(session_factory, mapping_ids: list[str]) -> int:
    count = 0
    for mapping_id in dict.fromkeys(mapping_ids):
        with session_factory() as session:
            mapping = _mapping_for_reconcile(session, mapping_id)
            if mapping is None:
                continue
            case = _reconcile_case(
                session,
                action_id=mapping.action_id,
                attempt_id=mapping.execution_attempt_id,
            )
            outcome = apply_remote_reconcile_evidence(
                session,
                case.id,
                _mapping_evidence(mapping),
                actor="telegram-update-collector",
                checked_at=mapping.observed_at,
            )
            session.commit()
            count += int(outcome.changed)
    return count


def _mapping_for_reconcile(session, mapping_id):
    mapping = session.get(TelegramOutboundRandomIdMapping, mapping_id)
    if mapping is None or not mapping.action_id or not mapping.execution_attempt_id:
        return None
    action = session.get(Action, mapping.action_id)
    if action is None or action.status != "unknown_after_send":
        return None
    if action.action_type == "group_clone_send":
        payload = GroupCloneSendPayload.model_validate(action.payload or {})
        if payload.media_items and not _complete_media_mappings(session, action, payload):
            return None
    return mapping


def _complete_media_mappings(session, action, payload) -> bool:
    identity_ids = [item.gateway_mutation_identity_id for item in payload.media_items]
    rows = session.scalars(select(TelegramOutboundRandomIdMapping).where(
        TelegramOutboundRandomIdMapping.action_id == action.id,
        TelegramOutboundRandomIdMapping.gateway_mutation_identity_id.in_(identity_ids),
    )).all()
    by_identity = {item.gateway_mutation_identity_id: item for item in rows}
    if any(identity_id not in by_identity for identity_id in identity_ids):
        return False
    action.result = {
        **dict(action.result or {}),
        "telegram_msg_ids": [
            by_identity[identity_id].remote_message_or_topic_id
            for identity_id in identity_ids
        ],
    }
    return True


def _reconcile_case(session, *, action_id: str, attempt_id: str) -> RemoteReconcileCase:
    case = session.scalar(select(RemoteReconcileCase).where(
        RemoteReconcileCase.action_id == action_id,
        RemoteReconcileCase.execution_attempt_id == attempt_id,
    ).with_for_update())
    if case is not None:
        return case
    action = session.get(Action, action_id)
    attempt = session.get(ExecutionAttempt, attempt_id)
    if action is None or attempt is None:
        raise RuntimeError("telegram_update_reconcile_runtime_missing")
    return ensure_remote_reconcile_case(session, action, attempt)


def _mapping_evidence(mapping) -> RemoteReconcileEvidence:
    return RemoteReconcileEvidence(
        result="remote_confirmed",
        source="telegram_update_message_id",
        evidence_fingerprint=canonical_state_hash({
            "state_id": mapping.authorization_update_state_id,
            "random_id": mapping.random_id,
            "remote_message_id": mapping.remote_message_or_topic_id,
            "update_identity_hash": mapping.update_identity_hash,
        }),
        remote_message_id=mapping.remote_message_or_topic_id,
        remote_mutation_started=True,
        exact_match_count=1,
    )


__all__ = ["reconcile_update_mappings"]
